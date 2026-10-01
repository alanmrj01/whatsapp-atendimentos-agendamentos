from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.domain import AccessCondition, BookingRequirements, ServiceAddress
from app.conversations.constants import ConversationState
from app.conversations.ports import BookingAvailabilityPort, BookingRequiresHandoff
from app.conversations.transitions import _context_from_existing_booking
from app.models import (
    Appointment,
    Conversation,
    Customer,
    Employee,
    EmployeeService,
    Message,
    Service,
)
from app.operations.schemas import (
    AppointmentRescheduleConflict,
    AppointmentRescheduleRequest,
    AppointmentRescheduleResult,
)
from app.whatsapp.templates import (
    RESCHEDULE_PREFERRED_TEMPLATE_NAME,
    RESCHEDULE_TEMPLATE_LANGUAGE,
    RESCHEDULE_TEMPLATE_NAME,
    render_reschedule_preferred_template_preview,
    render_reschedule_template_preview,
)


@dataclass(frozen=True, slots=True)
class AdminRescheduleOutcome:
    result: AppointmentRescheduleResult
    message_ids: tuple[UUID, ...]


async def initiate_admin_reschedule(
    session: AsyncSession,
    *,
    business_id: UUID,
    appointment_id: UUID,
    payload: AppointmentRescheduleRequest,
    booking_port: BookingAvailabilityPort,
    templates_enabled: bool = False,
) -> AdminRescheduleOutcome:
    appointment = await session.scalar(
        select(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.id == appointment_id,
        )
        .with_for_update()
    )
    if appointment is None:
        raise HTTPException(404, "Appointment not found")
    if appointment.status != "confirmed":
        raise HTTPException(409, "Only confirmed appointments can be rescheduled")

    service_window_open = await _customer_service_window_is_open(
        session,
        business_id=business_id,
        customer_id=appointment.customer_id,
    )
    if not service_window_open and not templates_enabled:
        raise HTTPException(
            409,
            "Customer service window is closed; approved reschedule templates are not enabled",
        )

    business_timezone = await _business_timezone(session, business_id)
    existing = next(
        (
            item
            for item in await booking_port.list_customer_bookings(
                business_id,
                appointment.customer_id,
            )
            if item.appointment_id == appointment.id
        ),
        None,
    )
    if existing is None:
        raise HTTPException(409, "Appointment is not available for rescheduling")

    context = _context_from_existing_booking(existing)
    requirements = existing.requirements
    original_details = dict(appointment.estimate_details or {})
    preferred = payload.preferred_starts_at
    conflicts: list[AppointmentRescheduleConflict] = []
    displaced: list[Appointment] = []

    # Releasing the original confirmed slot is intentional: a pending reschedule
    # must not continue consuming capacity in the calendar.
    _mark_reschedule_pending(
        appointment,
        original_details=original_details,
        preferred_starts_at=preferred,
        initiated_by="business",
    )
    await session.flush()

    if preferred is not None:
        local_preferred = preferred.astimezone(ZoneInfo(business_timezone))
        selected_date = local_preferred.date().isoformat()
        selected_time = local_preferred.strftime("%H:%M")
        available = await _slot_is_available(
            booking_port,
            business_id,
            appointment.service_id,
            selected_date,
            selected_time,
            requirements,
        )
        if not available:
            conflicts = await _find_appointment_conflicts(
                session,
                business_id=business_id,
                appointment=appointment,
                preferred_starts_at=preferred,
                requirements=requirements,
                booking_port=booking_port,
            )
            if not payload.force_conflicts or not conflicts:
                await session.rollback()
                return AdminRescheduleOutcome(
                    result=AppointmentRescheduleResult(
                        status="conflict",
                        appointment_id=appointment_id,
                        reason=(
                            "appointment_conflict"
                            if conflicts
                            else "slot_unavailable"
                        ),
                        conflicts=conflicts,
                    ),
                    message_ids=(),
                )

            # Client prioritization: release the minimum number of conflicting
            # confirmed appointments required to make one eligible technician
            # available. Each displaced customer enters the same pending
            # reschedule journey.
            for conflict_view in conflicts:
                conflict = await session.scalar(
                    select(Appointment)
                    .where(
                        Appointment.business_id == business_id,
                        Appointment.id == conflict_view.appointment_id,
                        Appointment.status == "confirmed",
                    )
                    .with_for_update()
                )
                if conflict is None:
                    continue
                conflict_window_open = await _customer_service_window_is_open(
                    session,
                    business_id=business_id,
                    customer_id=conflict.customer_id,
                )
                if not conflict_window_open and not templates_enabled:
                    continue
                _mark_reschedule_pending(
                    conflict,
                    original_details=dict(conflict.estimate_details or {}),
                    preferred_starts_at=None,
                    initiated_by="business_priority_displacement",
                    displaced_by=appointment_id,
                )
                displaced.append(conflict)
                await session.flush()
                if await _slot_is_available(
                    booking_port,
                    business_id,
                    appointment.service_id,
                    selected_date,
                    selected_time,
                    requirements,
                ):
                    break

            if not await _slot_is_available(
                booking_port,
                business_id,
                appointment.service_id,
                selected_date,
                selected_time,
                requirements,
            ):
                await session.rollback()
                return AdminRescheduleOutcome(
                    result=AppointmentRescheduleResult(
                        status="conflict",
                        appointment_id=appointment_id,
                        reason="slot_unavailable",
                        conflicts=conflicts,
                    ),
                    message_ids=(),
                )

        context["selected_date"] = selected_date
        context["selected_time"] = selected_time
        context["admin_preferred_reschedule"] = True

    message_ids: list[UUID] = []
    main_message = await _prepare_customer_reschedule(
        session,
        appointment=appointment,
        context=context,
        timezone_name=business_timezone,
        preferred_starts_at=preferred,
        displaced=False,
        use_template=not service_window_open,
    )
    if main_message is not None:
        message_ids.append(main_message.id)

    for conflict in displaced:
        displaced_existing = await _existing_booking_from_appointment(
            session,
            conflict,
            business_timezone,
        )
        displaced_context = _context_from_existing_booking(displaced_existing)
        conflict_window_open = await _customer_service_window_is_open(
            session,
            business_id=business_id,
            customer_id=conflict.customer_id,
        )
        displaced_message = await _prepare_customer_reschedule(
            session,
            appointment=conflict,
            context=displaced_context,
            timezone_name=business_timezone,
            preferred_starts_at=None,
            displaced=True,
            use_template=not conflict_window_open,
        )
        if displaced_message is not None:
            message_ids.append(displaced_message.id)

    await session.commit()
    return AdminRescheduleOutcome(
        result=AppointmentRescheduleResult(
            status="pending",
            appointment_id=appointment_id,
            displaced_appointment_ids=[item.id for item in displaced],
            message_ids=message_ids,
        ),
        message_ids=tuple(message_ids),
    )


async def list_pending_reschedules(
    session: AsyncSession,
    business_id: UUID,
) -> list[Appointment]:
    rows = await session.scalars(
        select(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.status == "pending",
            Appointment.estimate_details["reschedule_pending"].as_string() == "true",
        )
        .order_by(Appointment.updated_at.desc(), Appointment.id)
    )
    return list(rows.all())


async def _slot_is_available(
    booking_port: BookingAvailabilityPort,
    business_id: UUID,
    service_id: UUID,
    selected_date: str,
    selected_time: str,
    requirements: BookingRequirements,
) -> bool:
    try:
        options = await booking_port.list_times(
            business_id,
            service_id,
            selected_date,
            requirements,
        )
    except BookingRequiresHandoff:
        return False
    return any(option.id == selected_time for option in options)


async def _find_appointment_conflicts(
    session: AsyncSession,
    *,
    business_id: UUID,
    appointment: Appointment,
    preferred_starts_at: datetime,
    requirements: BookingRequirements,
    booking_port: BookingAvailabilityPort,
) -> list[AppointmentRescheduleConflict]:
    plan = await booking_port.estimate(
        business_id,
        appointment.service_id,
        requirements,
    )
    preferred_utc = preferred_starts_at.astimezone(UTC)
    service_end = preferred_utc + timedelta(
        minutes=plan.service.estimated_duration_minutes
    )
    proposed_start = preferred_utc - timedelta(minutes=plan.travel_before_minutes)
    proposed_end = service_end + timedelta(minutes=plan.travel_after_minutes)

    eligible_ids = list(
        (
            await session.scalars(
                select(Employee.id)
                .join(
                    EmployeeService,
                    and_(
                        EmployeeService.business_id == Employee.business_id,
                        EmployeeService.employee_id == Employee.id,
                    ),
                )
                .where(
                    Employee.business_id == business_id,
                    Employee.active.is_(True),
                    Employee.operational_role == "technician",
                    EmployeeService.service_id == appointment.service_id,
                )
            )
        ).all()
    )
    if not eligible_ids:
        return []

    rows = await session.execute(
        select(Appointment, Customer, Service)
        .join(
            Customer,
            and_(
                Customer.business_id == Appointment.business_id,
                Customer.id == Appointment.customer_id,
            ),
        )
        .join(
            Service,
            and_(
                Service.business_id == Appointment.business_id,
                Service.id == Appointment.service_id,
            ),
        )
        .where(
            Appointment.business_id == business_id,
            Appointment.status == "confirmed",
            Appointment.id != appointment.id,
            Appointment.employee_id.in_(eligible_ids),
            Appointment.starts_at < proposed_end + timedelta(hours=8),
            Appointment.ends_at > proposed_start - timedelta(hours=8),
        )
        .order_by(Appointment.starts_at, Appointment.id)
    )

    result: list[AppointmentRescheduleConflict] = []
    for other, customer, service in rows.all():
        occupied_start = other.starts_at - timedelta(
            minutes=int(other.travel_before_minutes or 0)
        )
        occupied_end = other.ends_at + timedelta(
            minutes=int(other.travel_after_minutes or 0)
        )
        if proposed_start < occupied_end and occupied_start < proposed_end:
            result.append(
                AppointmentRescheduleConflict(
                    appointment_id=other.id,
                    customer_name=(
                        customer.name
                        or customer.whatsapp_profile_name
                        or customer.phone_e164
                        or "Cliente"
                    ),
                    service_name=service.name,
                    starts_at=other.starts_at,
                )
            )
    return result


def _mark_reschedule_pending(
    appointment: Appointment,
    *,
    original_details: dict[str, Any],
    preferred_starts_at: datetime | None,
    initiated_by: str,
    displaced_by: UUID | None = None,
) -> None:
    details = dict(original_details)
    details.update(
        {
            "reschedule_pending": True,
            "reschedule_initiated_by": initiated_by,
            "reschedule_requested_at": datetime.now(UTC).isoformat(),
            "reschedule_original_starts_at": appointment.starts_at.isoformat(),
            "reschedule_original_employee_id": str(appointment.employee_id),
            "reschedule_preferred_starts_at": (
                preferred_starts_at.astimezone(UTC).isoformat()
                if preferred_starts_at is not None
                else None
            ),
        }
    )
    if displaced_by is not None:
        details["reschedule_displaced_by_appointment_id"] = str(displaced_by)
    appointment.status = "pending"
    appointment.estimate_details = details


async def _prepare_customer_reschedule(
    session: AsyncSession,
    *,
    appointment: Appointment,
    context: dict[str, Any],
    timezone_name: str,
    preferred_starts_at: datetime | None,
    displaced: bool,
    use_template: bool = False,
) -> Message | None:
    conversation = await session.scalar(
        select(Conversation)
        .where(
            Conversation.business_id == appointment.business_id,
            Conversation.customer_id == appointment.customer_id,
        )
        .with_for_update()
    )
    if conversation is None:
        conversation = Conversation(
            business_id=appointment.business_id,
            customer_id=appointment.customer_id,
            state=ConversationState.RESCHEDULE.value,
            context=context,
            automation_enabled=True,
            handoff_status="none",
            conversation_initiated_by="business",
        )
        session.add(conversation)
        await session.flush()
    else:
        conversation.state = ConversationState.RESCHEDULE.value
        conversation.context = context
        conversation.deleted_at = None
        conversation.last_interaction_at = datetime.now(UTC)
        conversation.conversation_initiated_by = "business"

    message_type = "text"
    outbound_payload: dict[str, Any] | None = None
    if use_template:
        customer = await session.scalar(
            select(Customer).where(
                Customer.business_id == appointment.business_id,
                Customer.id == appointment.customer_id,
            )
        )
        service = await session.scalar(
            select(Service).where(
                Service.business_id == appointment.business_id,
                Service.id == appointment.service_id,
            )
        )
        customer_name = (
            (customer.name or customer.whatsapp_profile_name)
            if customer is not None
            else None
        ) or "cliente"
        service_name = (service.name if service is not None else None) or "seu atendimento"
        current_slot = appointment.starts_at.astimezone(
            ZoneInfo(timezone_name)
        ).strftime("%d/%m/%Y às %H:%M")
        if preferred_starts_at is not None:
            preferred_slot = preferred_starts_at.astimezone(
                ZoneInfo(timezone_name)
            ).strftime("%d/%m/%Y às %H:%M")
            body = render_reschedule_preferred_template_preview(
                customer_name=customer_name,
                service_name=service_name,
                current_slot=current_slot,
                preferred_slot=preferred_slot,
            )
            outbound_payload = {
                "template_name": RESCHEDULE_PREFERRED_TEMPLATE_NAME,
                "language_code": RESCHEDULE_TEMPLATE_LANGUAGE,
                "body_parameters": [
                    customer_name,
                    service_name,
                    current_slot,
                    preferred_slot,
                ],
                "_alovia_template_purpose": "admin_reschedule",
            }
        else:
            body = render_reschedule_template_preview(
                customer_name=customer_name,
                service_name=service_name,
                current_slot=current_slot,
            )
            outbound_payload = {
                "template_name": RESCHEDULE_TEMPLATE_NAME,
                "language_code": RESCHEDULE_TEMPLATE_LANGUAGE,
                "body_parameters": [
                    customer_name,
                    service_name,
                    current_slot,
                ],
                "_alovia_template_purpose": (
                    "priority_displacement" if displaced else "admin_reschedule"
                ),
            }
        message_type = "template"
    elif preferred_starts_at is not None:
        local = preferred_starts_at.astimezone(ZoneInfo(timezone_name))
        body = (
            "Precisamos reagendar seu atendimento. "
            f"Temos preferência por {local.strftime('%d/%m às %H:%M')}. "
            "Esse dia e horário funcionam para você? "
            "Responda sim para confirmar ou diga que prefere outro horário."
        )
    elif displaced:
        body = (
            "Precisamos reorganizar sua agenda e reagendar este atendimento. "
            "Seu horário anterior foi liberado e não ocupa mais a agenda. "
            "Responda a esta mensagem e eu vou apresentar as novas datas e "
            "horários disponíveis."
        )
    else:
        body = (
            "Precisamos reagendar seu atendimento. Seu horário anterior foi "
            "liberado e não ocupa mais a agenda. Responda a esta mensagem e "
            "eu vou apresentar as novas datas e horários disponíveis."
        )

    details = (
        appointment.estimate_details
        if isinstance(appointment.estimate_details, dict)
        else {}
    )
    request_marker = details.get("reschedule_requested_at")
    if not isinstance(request_marker, str) or not request_marker.strip():
        raise HTTPException(409, "Reschedule request state is invalid")
    idempotency_key = (
        f"admin-reschedule:{appointment.business_id}:{appointment.id}:"
        f"{request_marker}"
    )
    existing_message = await session.scalar(
        select(Message).where(Message.idempotency_key == idempotency_key)
    )
    if existing_message is not None:
        return existing_message
    message = Message(
        business_id=appointment.business_id,
        conversation_id=conversation.id,
        provider_message_id=None,
        direction="outbound",
        message_type=message_type,
        body=body,
        interactive_id=None,
        outbound_payload=outbound_payload,
        status="pending",
        idempotency_key=idempotency_key,
    )
    session.add(message)
    await session.flush()
    return message


async def _existing_booking_from_appointment(
    session: AsyncSession,
    appointment: Appointment,
    timezone_name: str,
):
    from app.conversations.ports import ExistingBooking

    requirements = _requirements_from_appointment(appointment)
    service_name = await session.scalar(
        select(Service.name).where(
            Service.business_id == appointment.business_id,
            Service.id == appointment.service_id,
        )
    )
    local_start = appointment.starts_at.astimezone(ZoneInfo(timezone_name))
    return ExistingBooking(
        appointment_id=appointment.id,
        service_id=appointment.service_id,
        label=f"{service_name or 'Serviço'} · {local_start.strftime('%d/%m às %H:%M')}",
        requirements=requirements,
    )


def _requirements_from_appointment(
    appointment: Appointment,
) -> BookingRequirements:
    try:
        access = AccessCondition(appointment.access_condition or "normal")
    except ValueError:
        access = AccessCondition.UNKNOWN
    details = (
        appointment.estimate_details
        if isinstance(appointment.estimate_details, dict)
        else {}
    )
    operational = details.get("operational_details")
    if not isinstance(operational, dict):
        operational = {}
    site_start = _detail_time(operational.get("building_hours_start"))
    return BookingRequirements(
        quantity=appointment.quantity or 1,
        access_condition=access,
        address=ServiceAddress.from_snapshot(appointment.service_address),
        site_allowed_start=site_start,
        site_allowed_end=appointment.site_allowed_end,
        tubing_meters=appointment.tubing_meters,
        operational_details=dict(operational),
    )


def _detail_time(value: object):
    from datetime import time
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


async def _business_timezone(
    session: AsyncSession,
    business_id: UUID,
) -> str:
    from app.models import Business
    timezone_name = await session.scalar(
        select(Business.timezone).where(Business.id == business_id)
    )
    if not timezone_name:
        raise HTTPException(404, "Business not found")
    return timezone_name


async def _customer_service_window_is_open(
    session: AsyncSession,
    *,
    business_id: UUID,
    customer_id: UUID,
) -> bool:
    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.business_id == business_id,
            Conversation.customer_id == customer_id,
        )
    )
    if conversation_id is None:
        return False
    last_inbound_at = await session.scalar(
        select(func.max(Message.created_at)).where(
            Message.business_id == business_id,
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
    )
    return bool(
        last_inbound_at is not None
        and last_inbound_at + timedelta(hours=24) > datetime.now(UTC)
    )


async def _require_open_customer_service_window(
    session: AsyncSession,
    *,
    business_id: UUID,
    customer_id: UUID,
) -> None:
    if not await _customer_service_window_is_open(
        session,
        business_id=business_id,
        customer_id=customer_id,
    ):
        raise HTTPException(
            409,
            "Customer service window is closed; use an approved template",
        )

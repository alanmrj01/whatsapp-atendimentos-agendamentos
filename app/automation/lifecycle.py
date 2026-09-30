from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    Business,
    BusinessAutomationExclusion,
    Conversation,
    Customer,
    CustomerOutreach,
    Message,
    Service,
)

INACTIVITY_DELAY = timedelta(hours=24)
CLEANING_CYCLE_DELAY = timedelta(days=180)
TERMINAL_CONVERSATION_STATES = {
    "COMPLETED",
    "POST_BOOKING_HELP",
    "HUMAN_HANDOFF",
}
OUTREACH_BATCH_LIMIT = 100


async def create_due_lifecycle_outreach(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = OUTREACH_BATCH_LIMIT,
) -> list[uuid.UUID]:
    reference = now or datetime.now(timezone.utc)
    message_ids: list[uuid.UUID] = []
    message_ids.extend(
        await _create_incomplete_followups(
            session,
            reference=reference,
            limit=limit,
        )
    )
    remaining = max(0, limit - len(message_ids))
    if remaining:
        message_ids.extend(
            await _create_cleaning_followups(
                session,
                reference=reference,
                limit=remaining,
            )
        )
    return message_ids


async def _create_incomplete_followups(
    session: AsyncSession,
    *,
    reference: datetime,
    limit: int,
) -> list[uuid.UUID]:
    cutoff = reference - INACTIVITY_DELAY
    rows = (
        await session.execute(
            select(Conversation, Customer, Business)
            .join(
                Customer,
                and_(
                    Customer.business_id == Conversation.business_id,
                    Customer.id == Conversation.customer_id,
                ),
            )
            .join(Business, Business.id == Conversation.business_id)
            .where(
                Conversation.last_interaction_at <= cutoff,
                Conversation.state.not_in(TERMINAL_CONVERSATION_STATES),
                Conversation.automation_enabled.is_(True),
                Conversation.handoff_status == "none",
                Conversation.deleted_at.is_(None),
                or_(
                    Conversation.automation_suppressed_until.is_(None),
                    Conversation.automation_suppressed_until <= reference,
                ),
                Business.active.is_(True),
                Business.assistant_enabled.is_(True),
                ~exists(
                    select(BusinessAutomationExclusion.id).where(
                        BusinessAutomationExclusion.business_id == Conversation.business_id,
                        BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                        BusinessAutomationExclusion.active.is_(True),
                    )
                ),
            )
            .order_by(Conversation.last_interaction_at.asc())
            .limit(limit)
        )
    ).all()

    created: list[uuid.UUID] = []
    for conversation, customer, _business in rows:
        context = dict(conversation.context or {})
        if context.get("appointment_id"):
            continue
        trigger = conversation.last_interaction_at
        key = (
            f"outreach:incomplete24h:{conversation.id}:"
            f"{trigger.astimezone(timezone.utc).isoformat()}"
        )
        if await _outreach_exists(session, key):
            continue

        service_label = await _service_label_from_context(
            session,
            conversation.business_id,
            context,
        )
        subject = service_label or "o atendimento que você iniciou"
        outreach = CustomerOutreach(
            business_id=conversation.business_id,
            customer_id=conversation.customer_id,
            conversation_id=conversation.id,
            outreach_type="incomplete_24h",
            due_at=trigger + INACTIVITY_DELAY,
            trigger_at=trigger,
            status="pending",
            service_label=service_label,
            idempotency_key=key,
        )
        session.add(outreach)
        await session.flush()

        body = (
            f"Olá! Ficou alguma dúvida sobre {subject}? "
            "Se algo no serviço, no aparelho ou no valor não ficou como você esperava, "
            "me conte e eu ajudo a continuar seu atendimento."
        )
        message = _outreach_message(
            outreach,
            body=body,
            idempotency_suffix="message",
        )
        session.add(message)
        context["inactivity_followup_pending_response"] = True
        context["inactivity_followup_outreach_id"] = str(outreach.id)
        conversation.context = context
        await session.flush()
        created.append(message.id)
    return created


async def _create_cleaning_followups(
    session: AsyncSession,
    *,
    reference: datetime,
    limit: int,
) -> list[uuid.UUID]:
    cutoff = reference - CLEANING_CYCLE_DELAY
    latest_completed = (
        select(
            Appointment.business_id.label("business_id"),
            Appointment.customer_id.label("customer_id"),
            func.max(Appointment.ends_at).label("latest_ends_at"),
        )
        .where(Appointment.status == "completed")
        .group_by(Appointment.business_id, Appointment.customer_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Appointment, Service, Customer, Conversation, Business)
            .join(
                latest_completed,
                and_(
                    latest_completed.c.business_id == Appointment.business_id,
                    latest_completed.c.customer_id == Appointment.customer_id,
                    latest_completed.c.latest_ends_at == Appointment.ends_at,
                ),
            )
            .join(
                Service,
                and_(
                    Service.business_id == Appointment.business_id,
                    Service.id == Appointment.service_id,
                ),
            )
            .join(
                Customer,
                and_(
                    Customer.business_id == Appointment.business_id,
                    Customer.id == Appointment.customer_id,
                ),
            )
            .join(
                Conversation,
                and_(
                    Conversation.business_id == Appointment.business_id,
                    Conversation.customer_id == Appointment.customer_id,
                ),
            )
            .join(Business, Business.id == Appointment.business_id)
            .where(
                Appointment.status == "completed",
                Appointment.ends_at <= cutoff,
                Conversation.automation_enabled.is_(True),
                Conversation.handoff_status == "none",
                Conversation.deleted_at.is_(None),
                or_(
                    Conversation.automation_suppressed_until.is_(None),
                    Conversation.automation_suppressed_until <= reference,
                ),
                Business.active.is_(True),
                Business.assistant_enabled.is_(True),
                ~exists(
                    select(BusinessAutomationExclusion.id).where(
                        BusinessAutomationExclusion.business_id == Appointment.business_id,
                        BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                        BusinessAutomationExclusion.active.is_(True),
                    )
                ),
                ~exists(
                    select(Appointment.id).where(
                        Appointment.business_id == latest_completed.c.business_id,
                        Appointment.customer_id == latest_completed.c.customer_id,
                        Appointment.status == "confirmed",
                        Appointment.starts_at >= reference,
                    )
                ),
            )
            .order_by(Appointment.ends_at.asc())
            .limit(limit)
        )
    ).all()

    created: list[uuid.UUID] = []
    for appointment, service, customer, conversation, business in rows:
        key = f"outreach:cleaning6m:{appointment.id}"
        if await _outreach_exists(session, key):
            continue

        outreach = CustomerOutreach(
            business_id=appointment.business_id,
            customer_id=appointment.customer_id,
            conversation_id=conversation.id,
            source_appointment_id=appointment.id,
            outreach_type="cleaning_6m",
            due_at=appointment.ends_at + CLEANING_CYCLE_DELAY,
            trigger_at=appointment.ends_at,
            status="pending",
            service_label=service.name,
            idempotency_key=key,
        )
        session.add(outreach)
        await session.flush()

        zone = ZoneInfo(business.timezone)
        service_date = appointment.starts_at.astimezone(zone).strftime("%d/%m/%Y")
        display_name = (
            customer.name
            or customer.whatsapp_profile_name
            or "tudo bem"
        )
        greeting = (
            f"Olá, {display_name}! Aqui é a {business.name}. "
            f"No dia {service_date} fizemos seu atendimento de {service.name}."
        )
        care = (
            "Passando para lembrar que a limpeza periódica ajuda a evitar acúmulo de "
            "sujeira, mau cheiro e perda de eficiência. Aqui na "
            f"{business.name}, cuidamos dos nossos clientes como família. "
            "Se quiser, posso verificar um horário para uma limpeza."
        )
        group = f"outreach-{outreach.id.hex}"
        first = _outreach_message(
            outreach,
            body=greeting,
            idempotency_suffix="message:0",
            sequence_group=group,
            sequence_index=0,
            sequence_count=2,
        )
        second = _outreach_message(
            outreach,
            body=care,
            idempotency_suffix="message:1",
            sequence_group=group,
            sequence_index=1,
            sequence_count=2,
        )
        session.add_all((first, second))
        context = dict(conversation.context or {})
        context["cleaning_outreach_pending_response"] = True
        context["cleaning_outreach_id"] = str(outreach.id)
        conversation.context = context
        await session.flush()
        created.append(first.id)
    return created


async def _outreach_exists(session: AsyncSession, key: str) -> bool:
    existing = await session.scalar(
        select(CustomerOutreach.id).where(CustomerOutreach.idempotency_key == key)
    )
    return existing is not None


async def _service_label_from_context(
    session: AsyncSession,
    business_id: uuid.UUID,
    context: dict[str, object],
) -> str | None:
    raw = context.get("service_id")
    if not isinstance(raw, str):
        return None
    try:
        service_id = uuid.UUID(raw)
    except ValueError:
        return None
    return await session.scalar(
        select(Service.name).where(
            Service.business_id == business_id,
            Service.id == service_id,
        )
    )


def _outreach_message(
    outreach: CustomerOutreach,
    *,
    body: str,
    idempotency_suffix: str,
    sequence_group: str | None = None,
    sequence_index: int | None = None,
    sequence_count: int | None = None,
) -> Message:
    payload: dict[str, object] = {
        "_alovia_outreach_id": str(outreach.id),
        "_alovia_outreach_type": outreach.outreach_type,
    }
    if sequence_group is not None:
        payload["_alovia_sequence_group"] = sequence_group
        payload["_alovia_sequence_index"] = sequence_index
        payload["_alovia_sequence_count"] = sequence_count
    return Message(
        id=uuid.uuid4(),
        business_id=outreach.business_id,
        conversation_id=outreach.conversation_id,
        provider_message_id=None,
        direction="outbound",
        message_type="text",
        body=body,
        interactive_id=None,
        outbound_payload=payload,
        status="pending",
        idempotency_key=f"{outreach.idempotency_key}:{idempotency_suffix}",
    )


async def mark_outreach_response(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str | None,
    occurred_at: datetime,
) -> None:
    outreach = await session.scalar(
        select(CustomerOutreach)
        .where(
            CustomerOutreach.business_id == business_id,
            CustomerOutreach.conversation_id == conversation_id,
            CustomerOutreach.status.in_(("pending", "sent")),
        )
        .order_by(
            CustomerOutreach.sent_at.desc().nullslast(),
            CustomerOutreach.created_at.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if outreach is None:
        return

    if outreach.status == "pending":
        outreach.status = "skipped"
        outreach.responded_at = occurred_at
        await session.execute(
            update(Message)
            .where(
                Message.business_id == business_id,
                Message.conversation_id == conversation_id,
                Message.direction == "outbound",
                Message.status == "pending",
                Message.outbound_payload.op("->>")("_alovia_outreach_id")
                == str(outreach.id),
            )
            .values(status="failed")
        )
        return

    normalized = " ".join((body or "").casefold().split())
    declined = any(
        phrase in normalized
        for phrase in (
            "nao tenho interesse",
            "não tenho interesse",
            "nao quero",
            "não quero",
            "pode deixar",
            "agora nao",
            "agora não",
        )
    )
    outreach.status = "declined" if declined else "responded"
    outreach.responded_at = occurred_at


async def mark_outreach_sent(
    session: AsyncSession,
    *,
    outreach_id: uuid.UUID,
    sent_at: datetime | None = None,
) -> None:
    await session.execute(
        update(CustomerOutreach)
        .where(
            CustomerOutreach.id == outreach_id,
            CustomerOutreach.status == "pending",
        )
        .values(
            status="sent",
            sent_at=sent_at or func.now(),
        )
    )

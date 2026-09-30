from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, cast, exists, func, or_, select, update, String
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    Business,
    BusinessAutomationExclusion,
    CommercialAutomationEvent,
    Conversation,
    Customer,
    Message,
    Service,
)
from app.schemas.commercial_automation import (
    CleaningReminderDashboard,
    CleaningReminderUpcomingView,
    CommercialAutomationView,
)


ABANDONED_FOLLOWUP_TEMPLATE = "alovia_atendimento_pendente_24h"
CLEANING_GREETING_TEMPLATE = "alovia_limpeza_6m_apresentacao"
CLEANING_OFFER_TEMPLATE = "alovia_limpeza_6m_convite"
TEMPLATE_LANGUAGE = "pt_BR"
ABANDONED_DELAY = timedelta(hours=24)
MAX_SWEEP_ITEMS = 200
TERMINAL_CONVERSATION_STATES = {"COMPLETED", "POST_BOOKING_HELP", "HUMAN_HANDOFF"}
CLEANING_SAFE_STATES = {"START", "MENU", "COMPLETED", "POST_BOOKING_HELP"}


@dataclass(frozen=True, slots=True)
class CreatedAutomation:
    event_id: uuid.UUID
    message_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class _AbandonedCandidate:
    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    business_name: str
    context: dict[str, Any]
    latest_inbound_id: uuid.UUID
    latest_outbound_at: datetime


@dataclass(frozen=True, slots=True)
class _CleaningCandidate:
    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    business_name: str
    appointment_id: uuid.UUID
    service_name: str
    service_date: datetime
    due_at: datetime


def add_months(value: datetime, months: int) -> datetime:
    if months < 0:
        raise ValueError("months must be non-negative")
    target_month = value.month - 1 + months
    year = value.year + target_month // 12
    month = target_month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _display_name(
    customer_name: str | None,
    profile_name: str | None,
    phone: str | None,
) -> str:
    for value in (customer_name, profile_name, phone):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Cliente"


def _followup_subject(context: dict[str, Any], service_name: str | None) -> str:
    purchase_mode = context.get("purchase_mode")
    if purchase_mode == "purchase" or context.get("purchase_only") is True:
        return "compra do ar-condicionado"
    if purchase_mode == "both":
        return "compra e instalação do ar-condicionado"
    if isinstance(service_name, str) and service_name.strip():
        return service_name.strip()
    return "atendimento"


class CommercialAutomationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def abandoned_candidates(
        self,
        now: datetime,
        *,
        limit: int = MAX_SWEEP_ITEMS,
    ) -> list[_AbandonedCandidate]:
        cutoff = now - ABANDONED_DELAY
        latest_inbound_id = (
            select(Message.id)
            .where(
                Message.business_id == Conversation.business_id,
                Message.conversation_id == Conversation.id,
                Message.direction == "inbound",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        latest_inbound_at = (
            select(func.max(Message.created_at))
            .where(
                Message.business_id == Conversation.business_id,
                Message.conversation_id == Conversation.id,
                Message.direction == "inbound",
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        latest_outbound_at = (
            select(func.max(Message.created_at))
            .where(
                Message.business_id == Conversation.business_id,
                Message.conversation_id == Conversation.id,
                Message.direction == "outbound",
            )
            .correlate(Conversation)
            .scalar_subquery()
        )
        active_exclusion = exists(
            select(BusinessAutomationExclusion.id).where(
                BusinessAutomationExclusion.business_id == Conversation.business_id,
                BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                BusinessAutomationExclusion.active.is_(True),
            )
        )
        result = await self.session.execute(
            select(
                Conversation,
                Customer.name.label("customer_name"),
                Customer.whatsapp_profile_name.label("profile_name"),
                Customer.phone_e164.label("customer_phone"),
                Business.name.label("business_name"),
                latest_inbound_id.label("latest_inbound_id"),
                latest_outbound_at.label("latest_outbound_at"),
                latest_inbound_at.label("latest_inbound_at"),
            )
            .join(
                Customer,
                and_(
                    Customer.business_id == Conversation.business_id,
                    Customer.id == Conversation.customer_id,
                ),
            )
            .join(Business, Business.id == Conversation.business_id)
            .where(
                Business.active.is_(True),
                Business.assistant_enabled.is_(True),
                Conversation.automation_enabled.is_(True),
                Conversation.handoff_status == "none",
                ~Conversation.state.in_(TERMINAL_CONVERSATION_STATES),
                or_(
                    Conversation.automation_suppressed_until.is_(None),
                    Conversation.automation_suppressed_until <= now,
                ),
                ~active_exclusion,
                latest_inbound_id.is_not(None),
                latest_outbound_at.is_not(None),
                latest_outbound_at <= cutoff,
                latest_outbound_at > latest_inbound_at,
            )
            .order_by(latest_outbound_at.asc(), Conversation.id)
            .limit(limit)
        )
        candidates: list[_AbandonedCandidate] = []
        for row in result.all():
            conversation = row[0]
            if not isinstance(row.latest_inbound_id, uuid.UUID):
                continue
            if not isinstance(row.latest_outbound_at, datetime):
                continue
            if isinstance(conversation.context, dict) and conversation.context.get(
                "appointment_id"
            ):
                continue
            candidates.append(
                _AbandonedCandidate(
                    business_id=conversation.business_id,
                    customer_id=conversation.customer_id,
                    conversation_id=conversation.id,
                    customer_name=_display_name(
                        row.customer_name,
                        row.profile_name,
                        row.customer_phone,
                    ),
                    customer_phone=row.customer_phone,
                    business_name=row.business_name,
                    context=dict(conversation.context or {}),
                    latest_inbound_id=row.latest_inbound_id,
                    latest_outbound_at=row.latest_outbound_at,
                )
            )
        return candidates

    async def service_name(
        self,
        business_id: uuid.UUID,
        context: dict[str, Any],
    ) -> str | None:
        raw = context.get("service_id")
        if not isinstance(raw, str):
            return None
        try:
            service_id = uuid.UUID(raw)
        except ValueError:
            return None
        return await self.session.scalar(
            select(Service.name).where(
                Service.business_id == business_id,
                Service.id == service_id,
            )
        )

    async def create_abandoned_followup(
        self,
        candidate: _AbandonedCandidate,
        *,
        now: datetime,
    ) -> CreatedAutomation | None:
        event_id = uuid.uuid4()
        anchor = str(candidate.latest_inbound_id)
        service_name = await self.service_name(
            candidate.business_id,
            candidate.context,
        )
        subject = _followup_subject(candidate.context, service_name)
        due_at = candidate.latest_outbound_at + ABANDONED_DELAY

        insert_event = (
            postgresql_insert(CommercialAutomationEvent)
            .values(
                id=event_id,
                business_id=candidate.business_id,
                customer_id=candidate.customer_id,
                conversation_id=candidate.conversation_id,
                appointment_id=None,
                result_appointment_id=None,
                message_id=None,
                event_type="abandoned_followup_24h",
                anchor_key=anchor,
                status="queued",
                due_at=due_at,
                event_metadata={
                    "service_label": subject,
                    "template_name": ABANDONED_FOLLOWUP_TEMPLATE,
                },
            )
            .on_conflict_do_nothing(
                constraint="uq_commercial_automation_events_business_type_anchor"
            )
            .returning(CommercialAutomationEvent.id)
        )
        inserted = await self.session.scalar(insert_event)
        if inserted is None:
            return None

        message_id = uuid.uuid4()
        body = (
            f"Olá, {candidate.customer_name}. Ficou alguma dúvida ou teve algum "
            f"detalhe sobre {subject} que não ficou como esperava? Se ainda tiver "
            f"interesse em {subject}, responda esta mensagem e continuamos por aqui."
        )
        message = Message(
            id=message_id,
            business_id=candidate.business_id,
            conversation_id=candidate.conversation_id,
            provider_message_id=None,
            direction="outbound",
            message_type="template",
            body=body,
            outbound_payload={
                "template_name": ABANDONED_FOLLOWUP_TEMPLATE,
                "language_code": TEMPLATE_LANGUAGE,
                "body_parameters": [
                    candidate.customer_name,
                    subject,
                    subject,
                ],
                "_alovia_automation_kind": "abandoned_followup_24h",
                "_alovia_automation_event_id": str(event_id),
            },
            status="pending",
            idempotency_key=(
                f"commercial:abandoned_followup_24h:"
                f"{candidate.business_id}:{anchor}"
            ),
        )
        self.session.add(message)
        await self.session.execute(
            update(CommercialAutomationEvent)
            .where(CommercialAutomationEvent.id == event_id)
            .values(message_id=message_id)
        )

        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.business_id == candidate.business_id,
                Conversation.id == candidate.conversation_id,
            )
            .with_for_update()
        )
        if conversation is not None:
            context = dict(conversation.context or {})
            context["commercial_followup_event_id"] = str(event_id)
            context["commercial_followup_pending"] = True
            conversation.context = context
        return CreatedAutomation(event_id, (message_id,))

    async def cleaning_candidates(
        self,
        now: datetime,
        *,
        include_future_days: int = 0,
        limit: int = MAX_SWEEP_ITEMS,
    ) -> list[_CleaningCandidate]:
        ranked = (
            select(
                Appointment.business_id.label("business_id"),
                Appointment.customer_id.label("customer_id"),
                Appointment.id.label("appointment_id"),
                Appointment.starts_at.label("service_date"),
                Appointment.service_id.label("service_id"),
                func.row_number()
                .over(
                    partition_by=(
                        Appointment.business_id,
                        Appointment.customer_id,
                    ),
                    order_by=(Appointment.starts_at.desc(), Appointment.id.desc()),
                )
                .label("rank"),
            )
            .where(Appointment.status == "completed")
            .subquery()
        )
        active_exclusion = exists(
            select(BusinessAutomationExclusion.id).where(
                BusinessAutomationExclusion.business_id == Conversation.business_id,
                BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                BusinessAutomationExclusion.active.is_(True),
            )
        )
        horizon = now + timedelta(days=max(0, include_future_days))
        result = await self.session.execute(
            select(
                ranked.c.business_id,
                ranked.c.customer_id,
                ranked.c.appointment_id,
                ranked.c.service_date,
                Customer.name.label("customer_name"),
                Customer.whatsapp_profile_name.label("profile_name"),
                Customer.phone_e164.label("customer_phone"),
                Business.name.label("business_name"),
                Service.name.label("service_name"),
                Conversation.id.label("conversation_id"),
            )
            .join(
                Customer,
                and_(
                    Customer.business_id == ranked.c.business_id,
                    Customer.id == ranked.c.customer_id,
                ),
            )
            .join(Business, Business.id == ranked.c.business_id)
            .join(
                Service,
                and_(
                    Service.business_id == ranked.c.business_id,
                    Service.id == ranked.c.service_id,
                ),
            )
            .join(
                Conversation,
                and_(
                    Conversation.business_id == ranked.c.business_id,
                    Conversation.customer_id == ranked.c.customer_id,
                ),
            )
            .where(
                ranked.c.rank == 1,
                Business.active.is_(True),
                Business.assistant_enabled.is_(True),
                Conversation.automation_enabled.is_(True),
                Conversation.handoff_status == "none",
                Conversation.state.in_(CLEANING_SAFE_STATES),
                or_(
                    Conversation.automation_suppressed_until.is_(None),
                    Conversation.automation_suppressed_until <= now,
                ),
                ~active_exclusion,
            )
            .order_by(ranked.c.service_date.asc(), ranked.c.appointment_id)
            .limit(limit * 3)
        )
        candidates: list[_CleaningCandidate] = []
        for row in result.all():
            due_at = add_months(row.service_date, 6)
            if due_at > horizon:
                continue
            candidates.append(
                _CleaningCandidate(
                    business_id=row.business_id,
                    customer_id=row.customer_id,
                    conversation_id=row.conversation_id,
                    customer_name=_display_name(
                        row.customer_name,
                        row.profile_name,
                        row.customer_phone,
                    ),
                    customer_phone=row.customer_phone,
                    business_name=row.business_name,
                    appointment_id=row.appointment_id,
                    service_name=row.service_name,
                    service_date=row.service_date,
                    due_at=due_at,
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    async def create_cleaning_reminder(
        self,
        candidate: _CleaningCandidate,
        *,
        now: datetime,
    ) -> CreatedAutomation | None:
        event_id = uuid.uuid4()
        anchor = str(candidate.appointment_id)
        insert_event = (
            postgresql_insert(CommercialAutomationEvent)
            .values(
                id=event_id,
                business_id=candidate.business_id,
                customer_id=candidate.customer_id,
                conversation_id=candidate.conversation_id,
                appointment_id=candidate.appointment_id,
                result_appointment_id=None,
                message_id=None,
                event_type="cleaning_reminder_6m",
                anchor_key=anchor,
                status="queued",
                due_at=candidate.due_at,
                event_metadata={
                    "source_service_name": candidate.service_name,
                    "source_service_date": candidate.service_date.isoformat(),
                    "templates": [
                        CLEANING_GREETING_TEMPLATE,
                        CLEANING_OFFER_TEMPLATE,
                    ],
                },
            )
            .on_conflict_do_nothing(
                constraint="uq_commercial_automation_events_business_type_anchor"
            )
            .returning(CommercialAutomationEvent.id)
        )
        inserted = await self.session.scalar(insert_event)
        if inserted is None:
            return None

        sequence_group = f"commercial-cleaning-{event_id.hex}"
        service_date = candidate.service_date.astimezone(
            candidate.service_date.tzinfo or timezone.utc
        ).strftime("%d/%m/%Y")
        first_id = uuid.uuid4()
        second_id = uuid.uuid4()
        first = Message(
            id=first_id,
            business_id=candidate.business_id,
            conversation_id=candidate.conversation_id,
            provider_message_id=None,
            direction="outbound",
            message_type="template",
            body=(
                f"Olá, {candidate.customer_name}! Aqui é a {candidate.business_name}. "
                f"Atendemos você em {service_date} e queríamos saber como está seu "
                "ar-condicionado."
            ),
            outbound_payload={
                "template_name": CLEANING_GREETING_TEMPLATE,
                "language_code": TEMPLATE_LANGUAGE,
                "body_parameters": [
                    candidate.customer_name,
                    candidate.business_name,
                    service_date,
                ],
                "_alovia_automation_kind": "cleaning_reminder_6m",
                "_alovia_automation_event_id": str(event_id),
                "_alovia_sequence_group": sequence_group,
                "_alovia_sequence_index": 0,
                "_alovia_sequence_count": 2,
            },
            status="pending",
            idempotency_key=(
                f"commercial:cleaning_reminder_6m:"
                f"{candidate.business_id}:{anchor}:0"
            ),
        )
        second = Message(
            id=second_id,
            business_id=candidate.business_id,
            conversation_id=candidate.conversation_id,
            provider_message_id=None,
            direction="outbound",
            message_type="template",
            body=(
                "A limpeza periódica ajuda a reduzir acúmulo de sujeira, mau cheiro "
                "e perda de eficiência. Para nós, cada cliente faz parte da família, "
                "e queremos continuar cuidando do seu equipamento. Se quiser, podemos "
                "agendar uma limpeza."
            ),
            outbound_payload={
                "template_name": CLEANING_OFFER_TEMPLATE,
                "language_code": TEMPLATE_LANGUAGE,
                "body_parameters": [candidate.business_name],
                "_alovia_automation_kind": "cleaning_reminder_6m",
                "_alovia_automation_event_id": str(event_id),
                "_alovia_sequence_group": sequence_group,
                "_alovia_sequence_index": 1,
                "_alovia_sequence_count": 2,
            },
            status="pending",
            idempotency_key=(
                f"commercial:cleaning_reminder_6m:"
                f"{candidate.business_id}:{anchor}:1"
            ),
        )
        self.session.add_all((first, second))
        await self.session.execute(
            update(CommercialAutomationEvent)
            .where(CommercialAutomationEvent.id == event_id)
            .values(message_id=first_id)
        )

        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.business_id == candidate.business_id,
                Conversation.id == candidate.conversation_id,
            )
            .with_for_update()
        )
        if conversation is not None:
            context = dict(conversation.context or {})
            context["commercial_offer_event_id"] = str(event_id)
            context["commercial_offer_kind"] = "cleaning_reminder_6m"
            context["commercial_offer_pending"] = True
            conversation.context = context
        return CreatedAutomation(event_id, (first_id, second_id))

    async def mark_responded(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            update(CommercialAutomationEvent)
            .where(
                CommercialAutomationEvent.business_id == business_id,
                CommercialAutomationEvent.conversation_id == conversation_id,
                CommercialAutomationEvent.status.in_(("queued", "sent")),
            )
            .values(status="responded", resolved_at=func.now())
        )

    async def mark_resolution(
        self,
        event_id: uuid.UUID,
        status: str,
        *,
        result_appointment_id: uuid.UUID | None = None,
    ) -> None:
        if status not in {"declined", "accepted", "handoff"}:
            raise ValueError("Unsupported commercial automation resolution")
        values: dict[str, Any] = {
            "status": status,
            "resolved_at": func.now(),
        }
        if result_appointment_id is not None:
            values["result_appointment_id"] = result_appointment_id
        await self.session.execute(
            update(CommercialAutomationEvent)
            .where(CommercialAutomationEvent.id == event_id)
            .values(**values)
        )

    async def dashboard(
        self,
        business_id: uuid.UUID,
        now: datetime,
        *,
        upcoming_days: int = 90,
        history_limit: int = 200,
    ) -> CleaningReminderDashboard:
        upcoming_candidates = [
            candidate
            for candidate in await self.cleaning_candidates(
                now,
                include_future_days=upcoming_days,
                limit=MAX_SWEEP_ITEMS,
            )
            if candidate.business_id == business_id
            and candidate.due_at > now
        ]
        existing_anchors = set(
            (
                await self.session.scalars(
                    select(CommercialAutomationEvent.anchor_key).where(
                        CommercialAutomationEvent.business_id == business_id,
                        CommercialAutomationEvent.event_type
                        == "cleaning_reminder_6m",
                    )
                )
            ).all()
        )
        upcoming = [
            CleaningReminderUpcomingView(
                customer_id=item.customer_id,
                customer_name=item.customer_name,
                customer_phone=item.customer_phone,
                source_appointment_id=item.appointment_id,
                source_service_name=item.service_name,
                source_service_date=item.service_date,
                due_at=item.due_at,
            )
            for item in upcoming_candidates
            if str(item.appointment_id) not in existing_anchors
        ]

        rows = await self.session.execute(
            select(
                CommercialAutomationEvent,
                Customer.name.label("customer_name"),
                Customer.whatsapp_profile_name.label("profile_name"),
                Customer.phone_e164.label("customer_phone"),
                Service.name.label("service_name"),
                Appointment.starts_at.label("source_service_date"),
            )
            .join(
                Customer,
                and_(
                    Customer.business_id == CommercialAutomationEvent.business_id,
                    Customer.id == CommercialAutomationEvent.customer_id,
                ),
            )
            .outerjoin(
                Appointment,
                and_(
                    Appointment.business_id == CommercialAutomationEvent.business_id,
                    Appointment.id == CommercialAutomationEvent.appointment_id,
                ),
            )
            .outerjoin(
                Service,
                and_(
                    Service.business_id == Appointment.business_id,
                    Service.id == Appointment.service_id,
                ),
            )
            .where(
                CommercialAutomationEvent.business_id == business_id,
                CommercialAutomationEvent.event_type == "cleaning_reminder_6m",
            )
            .order_by(
                CommercialAutomationEvent.created_at.desc(),
                CommercialAutomationEvent.id.desc(),
            )
            .limit(history_limit)
        )
        history: list[CommercialAutomationView] = []
        for row in rows.all():
            event = row[0]
            history.append(
                CommercialAutomationView(
                    id=event.id,
                    event_type=event.event_type,
                    status=event.status,
                    customer_id=event.customer_id,
                    customer_name=_display_name(
                        row.customer_name,
                        row.profile_name,
                        row.customer_phone,
                    ),
                    customer_phone=row.customer_phone,
                    appointment_id=event.appointment_id,
                    result_appointment_id=event.result_appointment_id,
                    due_at=event.due_at,
                    sent_at=event.sent_at,
                    resolved_at=event.resolved_at,
                    source_service_name=row.service_name,
                    source_service_date=row.source_service_date,
                )
            )
        return CleaningReminderDashboard(
            upcoming=sorted(upcoming, key=lambda item: item.due_at),
            history=history,
        )


async def mark_commercial_automation_responded(
    session: AsyncSession,
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    await CommercialAutomationRepository(session).mark_responded(
        business_id,
        conversation_id,
    )

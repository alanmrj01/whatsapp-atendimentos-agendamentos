from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Business,
    BusinessAutomationExclusion,
    CommercialAutomationEvent,
    Conversation,
    Customer,
    Message,
    ProcessedWebhook,
)
from app.whatsapp.retry_policy import (
    MAX_OUTBOUND_RETRY_ATTEMPTS,
    OUTBOUND_RETRY_ATTEMPT_KEY,
    current_outbound_retry_attempt,
)


@dataclass(frozen=True, slots=True)
class StoredOutboundMessage:
    message_id: uuid.UUID
    business_id: uuid.UUID
    recipient: str
    message_type: str
    body: str | None
    outbound_payload: dict[str, Any] | None
    status: str
    provider_message_id: str | None
    automation_blocked: bool = False


def _automation_blocked_for_message(
    *,
    idempotency_key: str | None,
    outbound_payload: dict[str, Any] | None,
    active_ignore: bool,
    standard_automation_blocked: bool,
) -> bool:
    if active_ignore:
        return True

    is_manual = bool(
        idempotency_key and idempotency_key.startswith("manual:outbound:")
    )
    is_handoff_notification = bool(
        outbound_payload
        and outbound_payload.get("_alovia_transition") == "handoff"
    )
    if is_manual or is_handoff_notification:
        return False
    return standard_automation_blocked


class OutboundTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_message(
        self, message_id: uuid.UUID
    ) -> StoredOutboundMessage | None:
        active_exclusion = exists(
            select(BusinessAutomationExclusion.id).where(
                BusinessAutomationExclusion.business_id == Message.business_id,
                BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                BusinessAutomationExclusion.active.is_(True),
            )
        )
        active_ignore = exists(
            select(BusinessAutomationExclusion.id).where(
                BusinessAutomationExclusion.business_id == Message.business_id,
                BusinessAutomationExclusion.whatsapp_id == Customer.whatsapp_id,
                BusinessAutomationExclusion.active.is_(True),
                BusinessAutomationExclusion.mode == "ignore",
            )
        )
        standard_automation_blocked = or_(
            Conversation.automation_enabled.is_(False),
            Business.assistant_enabled.is_(False),
            Conversation.automation_suppressed_until > func.now(),
            active_exclusion,
        )
        result = await self.session.execute(
            select(
                Message,
                Customer.whatsapp_id,
                active_ignore.label("active_ignore"),
                standard_automation_blocked.label("standard_automation_blocked"),
            )
            .join(
                Conversation,
                and_(
                    Conversation.business_id == Message.business_id,
                    Conversation.id == Message.conversation_id,
                ),
            )
            .join(
                Customer,
                and_(
                    Customer.business_id == Conversation.business_id,
                    Customer.id == Conversation.customer_id,
                ),
            )
            .join(Business, Business.id == Message.business_id)
            .where(
                Message.id == message_id,
                Message.direction == "outbound",
            )
            .with_for_update(of=Message)
        )
        row = result.one_or_none()
        if row is None:
            return None
        message, recipient, active_ignore_value, standard_blocked_value = row
        automation_blocked = _automation_blocked_for_message(
            idempotency_key=message.idempotency_key,
            outbound_payload=message.outbound_payload,
            active_ignore=bool(active_ignore_value),
            standard_automation_blocked=bool(standard_blocked_value),
        )
        return StoredOutboundMessage(
            message_id=message.id,
            business_id=message.business_id,
            recipient=recipient,
            message_type=message.message_type,
            body=message.body,
            outbound_payload=message.outbound_payload,
            status=message.status,
            provider_message_id=message.provider_message_id,
            automation_blocked=automation_blocked,
        )

    async def list_pending_for_provider_message_ids(
        self,
        provider_message_ids: list[str],
    ) -> list[uuid.UUID]:
        if not provider_message_ids:
            return []
        inbound = aliased(Message, name="inbound_message")
        outbound = aliased(Message, name="outbound_message")
        result = await self.session.execute(
            select(outbound.id)
            .select_from(inbound)
            .join(
                outbound,
                and_(
                    outbound.business_id == inbound.business_id,
                    outbound.conversation_id == inbound.conversation_id,
                ),
            )
            .where(
                inbound.direction == "inbound",
                inbound.provider_message_id.in_(provider_message_ids),
                outbound.direction == "outbound",
                outbound.status == "pending",
                or_(
                    outbound.outbound_payload.is_(None),
                    outbound.outbound_payload.op("->>")(
                        "_alovia_sequence_index"
                    ).is_(None),
                    outbound.outbound_payload.op("->>")(
                        "_alovia_sequence_index"
                    )
                    == "0",
                ),
            )
            .distinct()
        )
        return list(result.scalars())

    async def list_pending_for_event_key(
        self,
        event_key: str,
    ) -> list[uuid.UUID]:
        result = await self.session.execute(
            select(ProcessedWebhook.provider_message_id).where(
                ProcessedWebhook.event_key == event_key,
                ProcessedWebhook.event_type.like("message.inbound.%"),
            )
        )
        provider_message_id = result.scalar_one_or_none()
        if provider_message_id is None:
            return []
        return await self.list_pending_for_provider_message_ids(
            [provider_message_id]
        )

    async def next_sequence_message_id(
        self,
        message_id: uuid.UUID,
    ) -> uuid.UUID | None:
        row = (
            await self.session.execute(
                select(
                    Message.business_id,
                    Message.conversation_id,
                    Message.status,
                    Message.outbound_payload,
                ).where(
                    Message.id == message_id,
                    Message.direction == "outbound",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        payload = row.outbound_payload
        if not isinstance(payload, dict):
            return None
        delivered = row.status in {"sent", "delivered", "read"}
        optional_failure = (
            row.status == "failed"
            and (
                payload.get("_alovia_optional") is True
                # Compatibilidade transitória com mensagens já persistidas
                # durante o desenvolvimento desta política.
                or payload.get("_alovia_sequence_optional") is True
            )
        )
        if not delivered and not optional_failure:
            return None
        group = payload.get("_alovia_sequence_group")
        index = payload.get("_alovia_sequence_index")
        count = payload.get("_alovia_sequence_count")
        if (
            not isinstance(group, str)
            or not isinstance(index, int)
            or not isinstance(count, int)
            or index < 0
            or count <= index + 1
        ):
            return None

        next_id = await self.session.scalar(
            select(Message.id)
            .where(
                Message.business_id == row.business_id,
                Message.conversation_id == row.conversation_id,
                Message.direction == "outbound",
                Message.status == "pending",
                Message.outbound_payload.op("->>")(
                    "_alovia_sequence_group"
                )
                == group,
                Message.outbound_payload.op("->>")(
                    "_alovia_sequence_index"
                )
                == str(index + 1),
            )
            .limit(1)
        )
        return next_id

    async def mark_retry_pending(
        self,
        message_id: uuid.UUID,
    ) -> int | None:
        message = await self.session.scalar(
            select(Message)
            .where(Message.id == message_id, Message.direction == "outbound")
            .with_for_update()
        )
        if message is None:
            return None
        payload = dict(message.outbound_payload or {})
        retry_attempt = current_outbound_retry_attempt(payload)
        if retry_attempt >= MAX_OUTBOUND_RETRY_ATTEMPTS:
            message.status = "failed"
            return None
        retry_attempt += 1
        payload[OUTBOUND_RETRY_ATTEMPT_KEY] = retry_attempt
        message.outbound_payload = payload
        message.status = "pending"
        return retry_attempt

    async def retry_candidate_for_event_key(
        self,
        event_key: str,
    ) -> tuple[uuid.UUID, int] | None:
        row = (
            await self.session.execute(
                select(Message.id, Message.outbound_payload)
                .join(
                    ProcessedWebhook,
                    ProcessedWebhook.provider_message_id
                    == Message.provider_message_id,
                )
                .where(
                    ProcessedWebhook.event_key == event_key,
                    ProcessedWebhook.event_type == "message.status.failed",
                    Message.direction == "outbound",
                    Message.status == "pending",
                )
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        retry_attempt = current_outbound_retry_attempt(row.outbound_payload)
        if retry_attempt <= 0:
            return None
        return row.id, retry_attempt

    async def retry_candidate_for_message_id(
        self,
        message_id: uuid.UUID,
    ) -> tuple[uuid.UUID, int] | None:
        row = (
            await self.session.execute(
                select(Message.id, Message.outbound_payload).where(
                    Message.id == message_id,
                    Message.direction == "outbound",
                    Message.status == "pending",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        retry_attempt = current_outbound_retry_attempt(row.outbound_payload)
        if retry_attempt <= 0:
            return None
        return row.id, retry_attempt

    async def mark_sent(
        self,
        message_id: uuid.UUID,
        provider_message_id: str,
    ) -> None:
        payload = await self.session.scalar(
            select(Message.outbound_payload).where(Message.id == message_id)
        )
        await self.session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                provider_message_id=provider_message_id,
                status="sent",
            )
        )
        if isinstance(payload, dict):
            event_id = payload.get("_alovia_automation_event_id")
            sequence_index = payload.get("_alovia_sequence_index")
            sequence_count = payload.get("_alovia_sequence_count")
            is_final = (
                not isinstance(sequence_index, int)
                or not isinstance(sequence_count, int)
                or sequence_index >= sequence_count - 1
            )
            if isinstance(event_id, str) and is_final:
                try:
                    parsed_event_id = uuid.UUID(event_id)
                except ValueError:
                    parsed_event_id = None
                if parsed_event_id is not None:
                    await self.session.execute(
                        update(CommercialAutomationEvent)
                        .where(CommercialAutomationEvent.id == parsed_event_id)
                        .values(status="sent", sent_at=func.now())
                    )

    async def mark_failed(self, message_id: uuid.UUID) -> None:
        payload = await self.session.scalar(
            select(Message.outbound_payload).where(Message.id == message_id)
        )
        await self.session.execute(
            update(Message).where(Message.id == message_id).values(status="failed")
        )
        if isinstance(payload, dict):
            event_id = payload.get("_alovia_automation_event_id")
            if isinstance(event_id, str):
                try:
                    parsed_event_id = uuid.UUID(event_id)
                except ValueError:
                    parsed_event_id = None
                if parsed_event_id is not None:
                    await self.session.execute(
                        update(CommercialAutomationEvent)
                        .where(
                            CommercialAutomationEvent.id == parsed_event_id,
                            CommercialAutomationEvent.status.in_(("queued", "sent")),
                        )
                        .values(status="failed", resolved_at=func.now())
                    )

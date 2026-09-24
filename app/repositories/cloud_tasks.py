from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.types import ConversationInput
from app.models import Conversation, Customer, Message, ProcessedWebhook


@dataclass(frozen=True, slots=True)
class StoredTaskEvent:
    event_key: str
    event_type: str
    provider_message_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class _InboundRow:
    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    provider_message_id: str
    message_type: str
    body: str | None
    interactive_id: str | None
    whatsapp_id: str
    created_at: datetime


class CloudTaskEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_event(self, event_key: str) -> StoredTaskEvent | None:
        result = await self.session.execute(
            select(ProcessedWebhook)
            .where(ProcessedWebhook.event_key == event_key)
            .with_for_update()
        )
        event = result.scalar_one_or_none()
        if event is None:
            return None
        return StoredTaskEvent(
            event_key=event.event_key,
            event_type=event.event_type,
            provider_message_id=event.provider_message_id,
            status=event.status,
        )

    async def mark_attempt_started(self, event_key: str) -> None:
        await self.session.execute(
            update(ProcessedWebhook)
            .where(ProcessedWebhook.event_key == event_key)
            .values(
                status="processing",
                attempts=ProcessedWebhook.attempts + 1,
            )
        )

    async def _load_inbound_row(
        self,
        provider_message_id: str,
    ) -> _InboundRow | None:
        result = await self.session.execute(
            select(
                Message.business_id,
                Conversation.customer_id,
                Message.conversation_id,
                Message.provider_message_id,
                Message.message_type,
                Message.body,
                Message.interactive_id,
                Customer.whatsapp_id,
                Message.created_at,
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
            .where(
                Message.provider_message_id == provider_message_id,
                Message.direction == "inbound",
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _InboundRow(
            business_id=row.business_id,
            customer_id=row.customer_id,
            conversation_id=row.conversation_id,
            provider_message_id=row.provider_message_id,
            message_type=row.message_type,
            body=row.body,
            interactive_id=row.interactive_id,
            whatsapp_id=row.whatsapp_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _as_input(row: _InboundRow, *, body: str | None = None) -> ConversationInput:
        return ConversationInput(
            business_id=row.business_id,
            customer_id=row.customer_id,
            conversation_id=row.conversation_id,
            provider_message_id=row.provider_message_id,
            message_type=row.message_type,
            body=row.body if body is None else body,
            interactive_id=row.interactive_id,
            whatsapp_id=row.whatsapp_id,
        )

    async def load_inbound(self, provider_message_id: str) -> ConversationInput | None:
        row = await self._load_inbound_row(provider_message_id)
        return self._as_input(row) if row is not None else None

    async def load_inbound_turn(
        self,
        provider_message_id: str,
        *,
        window_seconds: float = 3.0,
    ) -> ConversationInput | None:
        """Agrupa mensagens de texto contíguas e deixa somente a última tarefa responder."""
        current = await self._load_inbound_row(provider_message_id)
        if current is None:
            return None
        if current.message_type != "text" or current.interactive_id is not None:
            return self._as_input(current)

        newer = await self.session.scalar(
            select(Message.id)
            .where(
                Message.business_id == current.business_id,
                Message.conversation_id == current.conversation_id,
                Message.direction == "inbound",
                Message.message_type == "text",
                Message.provider_message_id.is_not(None),
                or_(
                    Message.created_at > current.created_at,
                    and_(
                        Message.created_at == current.created_at,
                        Message.provider_message_id > current.provider_message_id,
                    ),
                ),
                Message.created_at
                <= current.created_at + timedelta(seconds=window_seconds),
            )
            .order_by(
                Message.created_at.asc(),
                Message.provider_message_id.asc(),
            )
            .limit(1)
        )
        if newer is not None:
            return None

        last_outbound_at = await self.session.scalar(
            select(func.max(Message.created_at)).where(
                Message.business_id == current.business_id,
                Message.conversation_id == current.conversation_id,
                Message.direction == "outbound",
                Message.created_at <= current.created_at,
            )
        )
        query = select(
            Message.provider_message_id,
            Message.body,
            Message.created_at,
        ).where(
            Message.business_id == current.business_id,
            Message.conversation_id == current.conversation_id,
            Message.direction == "inbound",
            Message.message_type == "text",
            Message.provider_message_id.is_not(None),
            Message.created_at <= current.created_at,
        )
        if last_outbound_at is not None:
            query = query.where(Message.created_at > last_outbound_at)
        result = await self.session.execute(
            query.order_by(
                Message.created_at.desc(),
                Message.provider_message_id.desc(),
            ).limit(20)
        )

        collected: list[str] = []
        previous_time = current.created_at
        for item in result.all():
            gap = previous_time - item.created_at
            if gap.total_seconds() > window_seconds:
                break
            if isinstance(item.body, str) and item.body.strip():
                collected.append(item.body.strip())
            previous_time = item.created_at
        collected.reverse()
        combined_body = "\n".join(collected) if collected else current.body
        return self._as_input(current, body=combined_body)

    async def update_message_status(
        self,
        provider_message_id: str,
        message_status: str,
    ) -> None:
        await self.session.execute(
            update(Message)
            .where(Message.provider_message_id == provider_message_id)
            .values(status=message_status)
        )

    async def complete_event(self, event_key: str, event_status: str) -> None:
        await self.session.execute(
            update(ProcessedWebhook)
            .where(ProcessedWebhook.event_key == event_key)
            .values(status=event_status, processed_at=func.now())
        )

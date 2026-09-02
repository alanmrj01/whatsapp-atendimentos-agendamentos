from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.types import ConversationInput
from app.models import Conversation, Customer, Message, ProcessedWebhook


@dataclass(frozen=True, slots=True)
class StoredTaskEvent:
    event_key: str
    event_type: str
    provider_message_id: str | None
    status: str


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

    async def load_inbound(self, provider_message_id: str) -> ConversationInput | None:
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
        return ConversationInput(
            business_id=row.business_id,
            customer_id=row.customer_id,
            conversation_id=row.conversation_id,
            provider_message_id=row.provider_message_id,
            message_type=row.message_type,
            body=row.body,
            interactive_id=row.interactive_id,
            whatsapp_id=row.whatsapp_id,
        )

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

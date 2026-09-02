from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Conversation, Customer, Message, ProcessedWebhook


@dataclass(frozen=True, slots=True)
class StoredOutboundMessage:
    message_id: uuid.UUID
    recipient: str
    message_type: str
    body: str | None
    outbound_payload: dict[str, Any] | None
    status: str
    provider_message_id: str | None


class OutboundTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_message(
        self, message_id: uuid.UUID
    ) -> StoredOutboundMessage | None:
        result = await self.session.execute(
            select(Message, Customer.whatsapp_id)
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
                Message.id == message_id,
                Message.direction == "outbound",
            )
            .with_for_update(of=Message)
        )
        row = result.one_or_none()
        if row is None:
            return None
        message, recipient = row
        return StoredOutboundMessage(
            message_id=message.id,
            recipient=recipient,
            message_type=message.message_type,
            body=message.body,
            outbound_payload=message.outbound_payload,
            status=message.status,
            provider_message_id=message.provider_message_id,
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

    async def mark_sent(
        self,
        message_id: uuid.UUID,
        provider_message_id: str,
    ) -> None:
        await self.session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                provider_message_id=provider_message_id,
                status="sent",
            )
        )

    async def mark_failed(self, message_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Message).where(Message.id == message_id).values(status="failed")
        )

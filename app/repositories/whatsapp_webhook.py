from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.lifecycle import mark_outreach_response
from app.models import (
    Conversation,
    Customer,
    Message,
    ProcessedWebhook,
    WebPushEvent,
)
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.whatsapp.webhook import InboundMessageEvent, NormalizedWebhookEvent


def build_claim_event_statement(event: NormalizedWebhookEvent) -> Insert:
    return (
        postgresql_insert(ProcessedWebhook)
        .values(
            id=uuid.uuid4(),
            event_key=event.event_key,
            provider_message_id=event.provider_message_id,
            event_type=event.event_type,
            status="processing",
            attempts=1,
        )
        .on_conflict_do_nothing(constraint="uq_processed_webhooks_event_key")
        .returning(ProcessedWebhook.id)
    )


class WhatsAppWebhookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_event(self, event: NormalizedWebhookEvent) -> bool:
        result = await self.session.execute(build_claim_event_statement(event))
        return result.scalar_one_or_none() is not None

    async def get_event_status(self, event_key: str) -> str | None:
        result = await self.session.execute(
            select(ProcessedWebhook.status).where(
                ProcessedWebhook.event_key == event_key
            )
        )
        return result.scalar_one_or_none()

    async def queue_event(self, event_key: str) -> None:
        await self.session.execute(
            update(ProcessedWebhook)
            .where(ProcessedWebhook.event_key == event_key)
            .values(status="queued", processed_at=None)
        )

    async def find_business_id(
        self, meta_phone_number_id: str
    ) -> uuid.UUID | None:
        return await WhatsAppConnectionRepository(
            self.session
        ).find_business_id_by_phone_number_id(
            meta_phone_number_id
        )

    async def get_or_create_customer_id(
        self,
        business_id: uuid.UUID,
        whatsapp_id: str,
        whatsapp_profile_name: str | None = None,
    ) -> uuid.UUID:
        customer_id = uuid.uuid4()
        result = await self.session.execute(
            postgresql_insert(Customer)
            .values(
                id=customer_id,
                business_id=business_id,
                whatsapp_id=whatsapp_id,
                whatsapp_profile_name=whatsapp_profile_name,
            )
            .on_conflict_do_nothing(
                constraint="uq_customers_business_whatsapp"
            )
            .returning(Customer.id)
        )
        created_id = result.scalar_one_or_none()
        if created_id is not None:
            return created_id

        if whatsapp_profile_name is not None:
            await self.session.execute(
                update(Customer)
                .where(
                    Customer.business_id == business_id,
                    Customer.whatsapp_id == whatsapp_id,
                )
                .values(whatsapp_profile_name=whatsapp_profile_name)
            )

        existing = await self.session.execute(
            select(Customer.id).where(
                Customer.business_id == business_id,
                Customer.whatsapp_id == whatsapp_id,
            )
        )
        return existing.scalar_one()

    async def get_or_create_conversation_id(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        initiated_by: str = "customer",
    ) -> uuid.UUID:
        conversation_id = uuid.uuid4()
        result = await self.session.execute(
            postgresql_insert(Conversation)
            .values(
                id=conversation_id,
                business_id=business_id,
                customer_id=customer_id,
                state="START",
                context=text("'{}'::jsonb"),
                automation_enabled=True,
                handoff_status="none",
                last_interaction_at=func.now(),
                conversation_initiated_by=initiated_by,
            )
            .on_conflict_do_nothing(
                constraint="uq_conversations_business_customer"
            )
            .returning(Conversation.id)
        )
        created_id = result.scalar_one_or_none()
        if created_id is not None:
            return created_id

        resurrected = await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.customer_id == customer_id,
                Conversation.deleted_at.is_not(None),
            )
            .values(
                deleted_at=None,
                state="START",
                context=text("'{}'::jsonb"),
                automation_enabled=True,
                handoff_status="none",
                automation_suppressed_until=None,
                suppression_reason=None,
                human_control_started_at=None,
                last_human_message_at=None,
                pinned_at=None,
                manual_unread=False,
                conversation_initiated_by=initiated_by,
                last_interaction_at=func.now(),
            )
            .returning(Conversation.id)
        )
        resurrected_id = resurrected.scalar_one_or_none()
        if resurrected_id is not None:
            return resurrected_id

        existing = await self.session.scalar(
            select(Conversation.id).where(
                Conversation.business_id == business_id,
                Conversation.customer_id == customer_id,
            )
        )
        if existing is None:
            raise RuntimeError("Conversation disappeared during get-or-create")
        return existing

    async def touch_conversation(self, conversation_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_interaction_at=func.now())
        )

    async def persist_inbound_message(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        event: InboundMessageEvent,
    ) -> None:
        await self.session.execute(
            postgresql_insert(Message).values(
                id=uuid.uuid4(),
                business_id=business_id,
                conversation_id=conversation_id,
                provider_message_id=event.provider_message_id,
                direction="inbound",
                message_type=event.message_type,
                body=event.body,
                interactive_id=event.interactive_id,
                media_id=event.media_id,
                media_mime_type=event.media_mime_type,
                media_filename=event.media_filename,
                media_sha256=event.media_sha256,
                status="received",
                created_at=event.occurred_at or func.now(),
            )
        )
        await self.session.execute(
            postgresql_insert(WebPushEvent)
            .values(
                id=uuid.uuid4(),
                business_id=business_id,
                event_key=event.event_key,
                event_type="inbound_message",
                target_path=f"/app/conversas/{conversation_id}",
            )
            .on_conflict_do_nothing(
                constraint="uq_web_push_events_event_key"
            )
        )
        await mark_outreach_response(
            self.session,
            business_id=business_id,
            conversation_id=conversation_id,
            body=event.body,
            occurred_at=event.occurred_at or datetime.now(timezone.utc),
        )

    async def update_message_status(
        self,
        business_id: uuid.UUID,
        provider_message_id: str,
        message_status: str,
    ) -> None:
        await self.session.execute(
            update(Message)
            .where(
                Message.business_id == business_id,
                Message.provider_message_id == provider_message_id,
            )
            .values(status=message_status)
        )

    async def complete_event(self, event_key: str, event_status: str) -> None:
        await self.session.execute(
            update(ProcessedWebhook)
            .where(ProcessedWebhook.event_key == event_key)
            .values(status=event_status, processed_at=func.now())
        )

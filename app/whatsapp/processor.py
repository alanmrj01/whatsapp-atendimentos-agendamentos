from __future__ import annotations

import logging
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.whatsapp_webhook import WhatsAppWebhookRepository
from app.whatsapp.webhook import (
    InboundMessageEvent,
    MessageStatusEvent,
    NormalizedWebhookEvent,
)

logger = logging.getLogger(__name__)
PROCESSED_WEBHOOK_EVENT_KEY_CONSTRAINT = "uq_processed_webhooks_event_key"


class WebhookRepository(Protocol):
    async def claim_event(self, event: NormalizedWebhookEvent) -> bool: ...

    async def find_business_id(
        self, meta_phone_number_id: str
    ) -> uuid.UUID | None: ...

    async def get_or_create_customer_id(
        self, business_id: uuid.UUID, whatsapp_id: str
    ) -> uuid.UUID: ...

    async def get_or_create_conversation_id(
        self, business_id: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID: ...

    async def touch_conversation(self, conversation_id: uuid.UUID) -> None: ...

    async def persist_inbound_message(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        event: InboundMessageEvent,
    ) -> None: ...

    async def update_message_status(
        self,
        business_id: uuid.UUID,
        provider_message_id: str,
        message_status: str,
    ) -> None: ...

    async def complete_event(self, event_key: str, event_status: str) -> None: ...


class TransactionSession(Protocol):
    def begin(self) -> AbstractAsyncContextManager[Any]: ...


async def process_webhook_events(
    session: AsyncSession | TransactionSession,
    events: list[NormalizedWebhookEvent],
    repository: WebhookRepository | None = None,
) -> None:
    event_repository = repository or WhatsAppWebhookRepository(
        cast(AsyncSession, session)
    )

    for event in events:
        try:
            async with session.begin():
                is_new_event = await event_repository.claim_event(event)
                if not is_new_event:
                    logger.info("webhook_duplicate")
                    continue

                business_id = await event_repository.find_business_id(
                    event.meta_phone_number_id
                )
                if business_id is None:
                    await event_repository.complete_event(event.event_key, "ignored")
                    logger.info("webhook_business_not_found")
                    continue

                if isinstance(event, InboundMessageEvent):
                    customer_id = (
                        await event_repository.get_or_create_customer_id(
                            business_id, event.whatsapp_id
                        )
                    )
                    conversation_id = (
                        await event_repository.get_or_create_conversation_id(
                            business_id, customer_id
                        )
                    )
                    await event_repository.touch_conversation(conversation_id)
                    await event_repository.persist_inbound_message(
                        business_id, conversation_id, event
                    )
                elif isinstance(event, MessageStatusEvent):
                    await event_repository.update_message_status(
                        business_id,
                        event.provider_message_id,
                        event.message_status,
                    )

                await event_repository.complete_event(event.event_key, "processed")
        except IntegrityError as exc:
            if not is_duplicate_event_error(exc):
                raise
            logger.info("webhook_duplicate")


def is_duplicate_event_error(exc: IntegrityError) -> bool:
    original_error = exc.orig
    error_chain = (
        original_error,
        getattr(original_error, "__cause__", None),
        getattr(original_error, "__context__", None),
    )
    return any(
        getattr(error, "constraint_name", None)
        == PROCESSED_WEBHOOK_EVENT_KEY_CONSTRAINT
        for error in error_chain
        if error is not None
    )

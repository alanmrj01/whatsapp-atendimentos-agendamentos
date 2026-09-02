from __future__ import annotations

import logging
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.domain import AutomationDecision, ExclusionMode
from app.automation.service import (
    AutomationPolicyRepository,
    AutomationPolicyService,
)
from app.conversations.engine import ConversationEngine
from app.conversations.ports import BookingAvailabilityPort
from app.conversations.types import ConversationInput
from app.repositories.conversations import ConversationRepository
from app.repositories.automation import (
    AutomationRepository,
    ConversationAutomationControl,
)
from app.repositories.whatsapp_webhook import WhatsAppWebhookRepository
from app.whatsapp.webhook import (
    BusinessMessageEchoEvent,
    InboundMessageEvent,
    MessageStatusEvent,
    NormalizedWebhookEvent,
    is_individual_whatsapp_id,
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
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        initiated_by: str = "customer",
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


class QueuedWebhookRepository(WebhookRepository, Protocol):
    async def get_event_status(self, event_key: str) -> str | None: ...

    async def queue_event(self, event_key: str) -> None: ...


class TransactionSession(Protocol):
    def begin(self) -> AbstractAsyncContextManager[Any]: ...


class ConversationProcessor(Protocol):
    async def process(self, inbound: ConversationInput) -> bool: ...


class PermissiveAutomationRepository:
    """Compatibility seam for isolated repository unit tests."""

    async def get_active_exclusion_mode(
        self, business_id: uuid.UUID, whatsapp_id: str
    ) -> ExclusionMode | None:
        return None

    async def lock_conversation_control(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationAutomationControl:
        return ConversationAutomationControl(None, "none")

    async def mark_human_only(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        return None

    async def clear_human_only_marker(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        return None

    async def clear_temporary_suppression(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        return None

    async def activate_human_control(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> datetime:
        return occurred_at


def build_conversation_engine(
    session: AsyncSession,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationEngine:
    return ConversationEngine(
        ConversationRepository(session),
        booking_port=booking_port,
    )


async def process_webhook_events(
    session: AsyncSession | TransactionSession,
    events: list[NormalizedWebhookEvent],
    repository: WebhookRepository | None = None,
    conversation_engine: ConversationProcessor | None = None,
    booking_port: BookingAvailabilityPort | None = None,
    automation_repository: AutomationPolicyRepository | None = None,
) -> None:
    event_repository = repository or WhatsAppWebhookRepository(
        cast(AsyncSession, session)
    )
    active_conversation_engine = conversation_engine
    if active_conversation_engine is None and repository is None:
        active_conversation_engine = build_conversation_engine(
            cast(AsyncSession, session),
            booking_port,
        )
    policy = AutomationPolicyService(
        automation_repository
        or (
            AutomationRepository(cast(AsyncSession, session))
            if isinstance(session, AsyncSession)
            else PermissiveAutomationRepository()
        )
    )

    for event in events:
        if isinstance(
            event, (InboundMessageEvent, BusinessMessageEchoEvent)
        ) and not is_individual_whatsapp_id(event.whatsapp_id):
            logger.info("webhook_collective_ignored")
            continue
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
                    exclusion_mode = await policy.active_exclusion(
                        business_id, event.whatsapp_id
                    )
                    if exclusion_mode is ExclusionMode.IGNORE:
                        await event_repository.complete_event(
                            event.event_key, "ignored"
                        )
                        continue
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
                    decision = await policy.evaluate_customer_inbound(
                        business_id,
                        conversation_id,
                        exclusion_mode,
                    )
                    if (
                        decision is AutomationDecision.ALLOWED
                        and active_conversation_engine is not None
                    ):
                        await active_conversation_engine.process(
                            ConversationInput(
                                business_id=business_id,
                                customer_id=customer_id,
                                conversation_id=conversation_id,
                                provider_message_id=event.provider_message_id,
                                message_type=event.message_type,
                                body=event.body,
                                interactive_id=event.interactive_id,
                                whatsapp_id=event.whatsapp_id,
                            )
                        )
                    if decision is not AutomationDecision.ALLOWED:
                        await event_repository.complete_event(
                            event.event_key, "ignored"
                        )
                        continue
                elif isinstance(event, BusinessMessageEchoEvent):
                    exclusion_mode = await policy.active_exclusion(
                        business_id, event.whatsapp_id
                    )
                    if exclusion_mode is ExclusionMode.IGNORE:
                        await event_repository.complete_event(
                            event.event_key, "ignored"
                        )
                        continue
                    customer_id = (
                        await event_repository.get_or_create_customer_id(
                            business_id, event.whatsapp_id
                        )
                    )
                    conversation_id = (
                        await event_repository.get_or_create_conversation_id(
                            business_id,
                            customer_id,
                            "business",
                        )
                    )
                    if exclusion_mode is ExclusionMode.HUMAN_ONLY:
                        await policy.evaluate_customer_inbound(
                            business_id,
                            conversation_id,
                            exclusion_mode,
                        )
                    await policy.register_manual_business_message(
                        business_id,
                        conversation_id,
                        event.occurred_at,
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


async def persist_webhook_events_for_tasks(
    session: AsyncSession | TransactionSession,
    events: list[NormalizedWebhookEvent],
    repository: QueuedWebhookRepository | None = None,
    automation_repository: AutomationPolicyRepository | None = None,
) -> list[str]:
    event_repository = repository or WhatsAppWebhookRepository(
        cast(AsyncSession, session)
    )
    event_keys: list[str] = []
    policy = AutomationPolicyService(
        automation_repository
        or (
            AutomationRepository(cast(AsyncSession, session))
            if isinstance(session, AsyncSession)
            else PermissiveAutomationRepository()
        )
    )

    for event in events:
        if isinstance(
            event, (InboundMessageEvent, BusinessMessageEchoEvent)
        ) and not is_individual_whatsapp_id(event.whatsapp_id):
            logger.info("webhook_collective_ignored")
            continue

        should_enqueue = False
        try:
            async with session.begin():
                is_new_event = await event_repository.claim_event(event)
                if not is_new_event:
                    event_status = await event_repository.get_event_status(
                        event.event_key
                    )
                    should_enqueue = event_status in {"queued", "processing"}
                else:
                    business_id = await event_repository.find_business_id(
                        event.meta_phone_number_id
                    )
                    if business_id is None:
                        await event_repository.complete_event(
                            event.event_key,
                            "ignored",
                        )
                        logger.info("webhook_business_not_found")
                    else:
                        if isinstance(event, InboundMessageEvent):
                            exclusion_mode = await policy.active_exclusion(
                                business_id, event.whatsapp_id
                            )
                            if exclusion_mode is ExclusionMode.IGNORE:
                                await event_repository.complete_event(
                                    event.event_key,
                                    "ignored",
                                )
                                continue
                            customer_id = (
                                await event_repository.get_or_create_customer_id(
                                    business_id,
                                    event.whatsapp_id,
                                )
                            )
                            conversation_id = (
                                await event_repository.get_or_create_conversation_id(
                                    business_id,
                                    customer_id,
                                )
                            )
                            await event_repository.touch_conversation(
                                conversation_id
                            )
                            await event_repository.persist_inbound_message(
                                business_id,
                                conversation_id,
                                event,
                            )
                            decision = await policy.evaluate_customer_inbound(
                                business_id,
                                conversation_id,
                                exclusion_mode,
                            )
                            if decision is not AutomationDecision.ALLOWED:
                                await event_repository.complete_event(
                                    event.event_key,
                                    "ignored",
                                )
                                continue
                            await event_repository.queue_event(event.event_key)
                            should_enqueue = True
                        elif isinstance(event, BusinessMessageEchoEvent):
                            exclusion_mode = await policy.active_exclusion(
                                business_id, event.whatsapp_id
                            )
                            if exclusion_mode is ExclusionMode.IGNORE:
                                await event_repository.complete_event(
                                    event.event_key,
                                    "ignored",
                                )
                                continue
                            customer_id = (
                                await event_repository.get_or_create_customer_id(
                                    business_id,
                                    event.whatsapp_id,
                                )
                            )
                            conversation_id = (
                                await event_repository.get_or_create_conversation_id(
                                    business_id,
                                    customer_id,
                                    "business",
                                )
                            )
                            if exclusion_mode is ExclusionMode.HUMAN_ONLY:
                                await policy.evaluate_customer_inbound(
                                    business_id,
                                    conversation_id,
                                    exclusion_mode,
                                )
                            await policy.register_manual_business_message(
                                business_id,
                                conversation_id,
                                event.occurred_at,
                            )
                            await event_repository.complete_event(
                                event.event_key,
                                "processed",
                            )
                        else:
                            await event_repository.queue_event(event.event_key)
                            should_enqueue = True
        except IntegrityError as exc:
            if not is_duplicate_event_error(exc):
                raise
            logger.info("webhook_duplicate")
            async with session.begin():
                event_status = await event_repository.get_event_status(
                    event.event_key
                )
            should_enqueue = event_status in {"queued", "processing"}

        if should_enqueue and event.event_key not in event_keys:
            event_keys.append(event.event_key)

    return event_keys


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

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.outbound_tasks import (
    OutboundTaskRepository,
    StoredOutboundMessage,
)
from app.tasks.cloud_tasks import (
    CloudTasksOutboundEnqueuer,
    OutboundTaskEnqueuer,
)
from app.whatsapp.client import (
    WhatsAppConfigurationError,
    WhatsAppInvalidResponseError,
    WhatsAppPermanentError,
    WhatsAppTemporaryError,
    WhatsAppValidationError,
)
from app.whatsapp.webhook import (
    InboundMessageEvent,
    NormalizedWebhookEvent,
    is_individual_whatsapp_id,
)

TERMINAL_OUTBOUND_STATUSES = {"sent", "delivered", "read", "failed"}
OutboundTaskResult = Literal["sent", "failed", "skipped"]


class OutboundTaskTransientError(RuntimeError):
    """Falha sanitizada que deve ser repetida pelo Cloud Tasks."""


class OutboundRepository(Protocol):
    async def lock_message(
        self, message_id: uuid.UUID
    ) -> StoredOutboundMessage | None: ...

    async def mark_sent(
        self, message_id: uuid.UUID, provider_message_id: str
    ) -> None: ...

    async def mark_failed(self, message_id: uuid.UUID) -> None: ...


class PendingOutboundRepository(Protocol):
    async def list_pending_for_provider_message_ids(
        self, provider_message_ids: list[str]
    ) -> list[uuid.UUID]: ...

    async def list_pending_for_event_key(
        self, event_key: str
    ) -> list[uuid.UUID]: ...


class TransactionSession(Protocol):
    def begin(self) -> AbstractAsyncContextManager[Any]: ...


class WhatsAppSender(Protocol):
    async def send_text(self, to: str, text: str) -> str: ...

    async def send_interactive_buttons(
        self,
        to: str,
        body: str,
        buttons: Sequence[Mapping[str, Any]],
    ) -> str: ...

    async def send_interactive_list(
        self,
        to: str,
        body: str,
        sections: Sequence[Mapping[str, Any]],
    ) -> str: ...

    async def aclose(self) -> None: ...


class BusinessSenderResolver(Protocol):
    async def resolve(self, business_id: uuid.UUID) -> WhatsAppSender: ...


WhatsAppSenderFactory = Callable[[], WhatsAppSender]


def build_outbound_task_enqueuer(settings: Settings) -> OutboundTaskEnqueuer:
    return CloudTasksOutboundEnqueuer(
        settings.require_outbound_tasks_configuration()
    )


async def enqueue_pending_outbounds_for_events(
    session: AsyncSession | TransactionSession,
    events: list[NormalizedWebhookEvent],
    enqueuer: OutboundTaskEnqueuer,
    repository: PendingOutboundRepository | None = None,
) -> list[uuid.UUID]:
    provider_message_ids = [
        event.provider_message_id
        for event in events
        if isinstance(event, InboundMessageEvent)
        and is_individual_whatsapp_id(event.whatsapp_id)
    ]
    if not provider_message_ids:
        return []
    pending_repository = repository or OutboundTaskRepository(
        cast(AsyncSession, session)
    )
    async with session.begin():
        message_ids = (
            await pending_repository.list_pending_for_provider_message_ids(
                provider_message_ids
            )
        )
    await enqueue_outbound_message_ids(message_ids, enqueuer)
    return message_ids


async def enqueue_pending_outbounds_for_event(
    session: AsyncSession | TransactionSession,
    event_key: str,
    enqueuer: OutboundTaskEnqueuer,
    repository: PendingOutboundRepository | None = None,
) -> list[uuid.UUID]:
    pending_repository = repository or OutboundTaskRepository(
        cast(AsyncSession, session)
    )
    async with session.begin():
        message_ids = await pending_repository.list_pending_for_event_key(
            event_key
        )
    await enqueue_outbound_message_ids(message_ids, enqueuer)
    return message_ids


async def enqueue_outbound_message_ids(
    message_ids: list[uuid.UUID],
    enqueuer: OutboundTaskEnqueuer,
) -> None:
    for message_id in dict.fromkeys(message_ids):
        await enqueuer.enqueue(message_id)


async def process_outbound_message(
    session: AsyncSession | TransactionSession,
    message_id: uuid.UUID,
    client_factory: WhatsAppSenderFactory | None = None,
    repository: OutboundRepository | None = None,
    *,
    sender_resolver: BusinessSenderResolver | None = None,
) -> OutboundTaskResult:
    outbound_repository = repository or OutboundTaskRepository(
        cast(AsyncSession, session)
    )
    async with session.begin():
        message = await outbound_repository.lock_message(message_id)
        if message is None:
            return "skipped"
        if (
            message.status in TERMINAL_OUTBOUND_STATUSES
            or message.status != "pending"
        ):
            return "skipped"
        if message.automation_blocked:
            await outbound_repository.mark_failed(message.message_id)
            return "failed"
        if not is_individual_whatsapp_id(message.recipient):
            await outbound_repository.mark_failed(message.message_id)
            return "failed"

        try:
            if sender_resolver is not None:
                client = await sender_resolver.resolve(message.business_id)
            elif client_factory is not None:
                client = client_factory()
            else:
                raise WhatsAppConfigurationError(
                    "WhatsApp sender resolver is not configured"
                )
        except WhatsAppConfigurationError:
            raise OutboundTaskTransientError(
                "WhatsApp sender is not configured"
            ) from None

        try:
            provider_message_id = await _send_message(client, message)
        except (WhatsAppTemporaryError, WhatsAppInvalidResponseError):
            raise OutboundTaskTransientError(
                "WhatsApp outbound delivery is temporarily unavailable"
            ) from None
        except (WhatsAppPermanentError, WhatsAppValidationError):
            await outbound_repository.mark_failed(message.message_id)
            return "failed"
        finally:
            await _close_sender(client)

        await outbound_repository.mark_sent(
            message.message_id,
            provider_message_id,
        )
        return "sent"


async def _send_message(
    client: WhatsAppSender,
    message: StoredOutboundMessage,
) -> str:
    body = cast(str, message.body)
    if message.message_type == "text":
        return await client.send_text(message.recipient, body)

    payload = message.outbound_payload
    if not isinstance(payload, Mapping):
        raise WhatsAppValidationError("Outbound payload is invalid")
    if message.message_type == "interactive_button":
        buttons = cast(Sequence[Mapping[str, Any]], payload.get("buttons"))
        return await client.send_interactive_buttons(
            message.recipient,
            body,
            buttons,
        )
    if message.message_type == "interactive_list":
        sections = cast(Sequence[Mapping[str, Any]], payload.get("sections"))
        return await client.send_interactive_list(
            message.recipient,
            body,
            sections,
        )
    raise WhatsAppValidationError("Outbound message type is invalid")


async def _close_sender(client: WhatsAppSender) -> None:
    try:
        await client.aclose()
    except Exception:
        return

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.ports import BookingAvailabilityPort
from app.conversations.types import ConversationInput
from app.repositories.cloud_tasks import (
    CloudTaskEventRepository,
    StoredTaskEvent,
)
from app.whatsapp.processor import ConversationProcessor, build_conversation_engine
from app.whatsapp.webhook import SUPPORTED_MESSAGE_STATUSES


class TaskEventDataUnavailable(RuntimeError):
    """Evento persistido ainda não possui os dados necessários."""


class TaskWorkerRepository(Protocol):
    async def lock_event(self, event_key: str) -> StoredTaskEvent | None: ...

    async def mark_attempt_started(self, event_key: str) -> None: ...

    async def load_inbound(
        self, provider_message_id: str
    ) -> ConversationInput | None: ...

    async def update_message_status(
        self, provider_message_id: str, message_status: str
    ) -> None: ...

    async def complete_event(self, event_key: str, event_status: str) -> None: ...


class TransactionSession(Protocol):
    def begin(self) -> AbstractAsyncContextManager[Any]: ...


async def process_cloud_task_event(
    session: AsyncSession | TransactionSession,
    event_key: str,
    repository: TaskWorkerRepository | None = None,
    conversation_engine: ConversationProcessor | None = None,
    booking_port: BookingAvailabilityPort | None = None,
) -> bool:
    event_repository = repository or CloudTaskEventRepository(
        cast(AsyncSession, session)
    )
    async with session.begin():
        event = await event_repository.lock_event(event_key)
        if event is None or event.status in {"processed", "ignored"}:
            return False

        await event_repository.mark_attempt_started(event_key)
        if event.event_type.startswith("message.inbound."):
            if event.provider_message_id is None:
                raise TaskEventDataUnavailable("Task event data is unavailable")
            inbound = await event_repository.load_inbound(
                event.provider_message_id
            )
            if inbound is None:
                raise TaskEventDataUnavailable("Task event data is unavailable")
            active_engine = conversation_engine or build_conversation_engine(
                cast(AsyncSession, session),
                booking_port,
            )
            await active_engine.process(inbound)
            await event_repository.complete_event(event_key, "processed")
            return True

        status_prefix = "message.status."
        if event.event_type.startswith(status_prefix):
            message_status = event.event_type.removeprefix(status_prefix)
            if (
                event.provider_message_id is not None
                and message_status in SUPPORTED_MESSAGE_STATUSES
            ):
                await event_repository.update_message_status(
                    event.provider_message_id,
                    message_status,
                )
                await event_repository.complete_event(event_key, "processed")
                return True

        await event_repository.complete_event(event_key, "ignored")
        return False

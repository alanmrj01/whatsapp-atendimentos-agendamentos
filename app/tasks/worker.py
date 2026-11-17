from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from uuid import UUID
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.domain import AutomationDecision
from app.automation.service import (
    AutomationPolicyRepository,
    AutomationPolicyService,
)
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from app.conversations.ports import BookingAvailabilityPort
from app.conversations.types import ConversationInput
from app.core.config import get_settings
from app.repositories.cloud_tasks import (
    CloudTaskEventRepository,
    StoredTaskEvent,
)
from app.repositories.automation import AutomationRepository
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.speech.transcription import (
    SpeechTranscriptionError,
    transcribe_short_audio,
)
from app.whatsapp.client import WhatsAppClientError
from app.whatsapp.processor import (
    ConversationProcessor,
    PermissiveAutomationRepository,
    build_conversation_engine,
)
from app.whatsapp.sender import build_business_sender_resolver
from app.whatsapp.webhook import SUPPORTED_MESSAGE_STATUSES


logger = logging.getLogger(__name__)
AudioInputResolver = Callable[[ConversationInput], Awaitable[ConversationInput]]


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

    async def complete_event(
        self,
        event_key: str,
        event_status: str,
        business_id: UUID | None = None,
        outcome_reason: str | None = None,
    ) -> None: ...


class TransactionSession(Protocol):
    def begin(self) -> AbstractAsyncContextManager[Any]: ...


async def process_cloud_task_event(
    session: AsyncSession | TransactionSession,
    event_key: str,
    repository: TaskWorkerRepository | None = None,
    conversation_engine: ConversationProcessor | None = None,
    booking_port: BookingAvailabilityPort | None = None,
    automation_repository: AutomationPolicyRepository | None = None,
    audio_input_resolver: AudioInputResolver | None = None,
) -> bool:
    event_repository = repository or CloudTaskEventRepository(
        cast(AsyncSession, session)
    )
    policy = AutomationPolicyService(
        automation_repository
        or (
            AutomationRepository(cast(AsyncSession, session))
            if isinstance(session, AsyncSession)
            else PermissiveAutomationRepository()
        )
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
            latest_inbound = inbound
            turn_loader = getattr(event_repository, "load_inbound_turn", None)
            if callable(turn_loader):
                aggregated = await turn_loader(event.provider_message_id)
                if aggregated is None:
                    await event_repository.complete_event(
                        event_key,
                        "processed",
                        event.business_id,
                        "debounced_fragment_merged",
                    )
                    return False
                inbound = _prefer_latest_substantive_fragment(
                    latest_inbound,
                    aggregated,
                )
            if inbound.whatsapp_id is None and isinstance(session, AsyncSession):
                await event_repository.complete_event(
                    event_key,
                    "ignored",
                    inbound.business_id,
                    "inbound_identity_unavailable",
                )
                return False
            if inbound.whatsapp_id is not None:
                exclusion_mode = await policy.active_exclusion(
                    inbound.business_id,
                    inbound.whatsapp_id,
                )
                decision = await policy.evaluate_customer_inbound(
                    inbound.business_id,
                    inbound.conversation_id,
                    exclusion_mode,
                )
                if decision is not AutomationDecision.ALLOWED:
                    await event_repository.complete_event(
                        event_key,
                        "ignored",
                        inbound.business_id,
                        decision.value,
                    )
                    return False
            if inbound.message_type == "audio":
                if audio_input_resolver is not None:
                    inbound = await audio_input_resolver(inbound)
                elif isinstance(session, AsyncSession):
                    inbound = await _transcribe_production_audio(
                        session,
                        inbound,
                    )

            active_engine = conversation_engine or build_conversation_engine(
                cast(AsyncSession, session),
                booking_port,
            )
            await active_engine.process(inbound)
            await event_repository.complete_event(
                event_key,
                "processed",
                inbound.business_id,
                "automation_processed",
            )
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
                await event_repository.complete_event(
                    event_key,
                    "processed",
                    event.business_id,
                    "message_status_processed",
                )
                return True

        await event_repository.complete_event(
            event_key,
            "ignored",
            event.business_id,
            "unsupported_event_type",
        )
        return False



async def _transcribe_production_audio(
    session: AsyncSession,
    inbound: ConversationInput,
) -> ConversationInput:
    """Transforma áudio WhatsApp em texto apenas para interpretação automática.

    O Message original continua persistido como áudio. Em qualquer falha
    esperada de Meta ou Speech, o input original é devolvido para que o motor
    aplique o fallback seguro já existente.
    """
    if inbound.message_type != "audio" or not inbound.media_id:
        return inbound

    resolver = build_business_sender_resolver(
        WhatsAppConnectionRepository(session),
        get_settings(),
    )
    client = None
    try:
        client = await resolver.resolve(inbound.business_id)
        audio_bytes, resolved_mime = await client.download_media(
            inbound.media_id
        )
        normalized_mime = (resolved_mime or "").split(";", 1)[0].strip().lower()
        if normalized_mime and not (
            normalized_mime.startswith("audio/")
            or normalized_mime == "application/octet-stream"
        ):
            logger.warning(
                "whatsapp_audio_media_type_rejected",
                extra={"business_id": str(inbound.business_id)},
            )
            return inbound

        transcript = " ".join(
            (await transcribe_short_audio(audio_bytes)).split()
        ).strip()
        if not transcript:
            return inbound

        logger.info(
            "whatsapp_audio_transcribed",
            extra={"business_id": str(inbound.business_id)},
        )
        return replace(
            inbound,
            message_type="text",
            body=transcript,
            interactive_id=None,
        )
    except (WhatsAppClientError, SpeechTranscriptionError) as exc:
        logger.warning(
            "whatsapp_audio_transcription_failed",
            extra={
                "business_id": str(inbound.business_id),
                "error_type": type(exc).__name__,
            },
        )
        return inbound
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

def _prefer_latest_substantive_fragment(
    latest: ConversationInput,
    aggregated: ConversationInput,
) -> ConversationInput:
    """Do not let turn aggregation erase a clear intent from the latest fragment."""

    if (
        latest.message_type != "text"
        or aggregated.message_type != "text"
        or not isinstance(latest.body, str)
        or not latest.body.strip()
        or latest.body == aggregated.body
    ):
        return aggregated

    interpreter = DeterministicConversationInterpreter()
    latest_interpretation = interpreter.interpret(latest.body)
    aggregated_interpretation = interpreter.interpret(aggregated.body)
    non_substantive = {
        ConversationIntent.UNKNOWN,
        ConversationIntent.GREETING,
    }
    if (
        latest_interpretation.intent not in non_substantive
        and aggregated_interpretation.intent in non_substantive
    ):
        return latest
    return aggregated

from __future__ import annotations

import hashlib
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Protocol

from app.conversations.constants import ConversationState
from app.conversations.ports import (
    BookingAvailabilityPort,
    BookingRecoveryRequired,
)
from app.conversations.transitions import (
    determine_transition,
    recover_booking_issue,
)
from app.conversations.types import (
    ConversationInput,
    ConversationSnapshot,
    ConversationTransition,
)


class ConversationEngineRepository(Protocol):
    def lock_conversation(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AbstractAsyncContextManager[ConversationSnapshot]: ...

    async def outbound_exists(self, idempotency_key: str) -> bool: ...

    async def persist_transition(
        self,
        snapshot: ConversationSnapshot,
        transition: ConversationTransition,
        idempotency_key: str,
    ) -> bool: ...


class ConversationEngine:
    def __init__(
        self,
        repository: ConversationEngineRepository,
        booking_port: BookingAvailabilityPort | None = None,
    ) -> None:
        self.repository = repository
        self.booking_port = booking_port

    async def process(self, inbound: ConversationInput) -> bool:
        async with self.repository.lock_conversation(
            inbound.business_id,
            inbound.conversation_id,
        ) as conversation:
            if (
                not conversation.assistant_enabled
                or not conversation.automation_enabled
            ):
                return False

            idempotency_key = build_outbound_idempotency_key(inbound)
            if await self.repository.outbound_exists(idempotency_key):
                return False

            try:
                transition = await determine_transition(
                    conversation,
                    inbound,
                    self.booking_port,
                )
            except BookingRecoveryRequired as exc:
                if self.booking_port is None:
                    return False
                try:
                    recovery_state = ConversationState(conversation.state)
                except ValueError:
                    recovery_state = ConversationState.START
                transition = await recover_booking_issue(
                    inbound,
                    self.booking_port,
                    recovery_state,
                    conversation.context,
                    exc,
                )
            if transition is None:
                return False
            if transition.state is ConversationState.HUMAN_HANDOFF:
                transition = replace(
                    transition,
                    outbound=replace(
                        transition.outbound,
                        body=conversation.handoff_message,
                    ),
                )
            return await self.repository.persist_transition(
                conversation,
                transition,
                idempotency_key,
            )


def build_outbound_idempotency_key(inbound: ConversationInput) -> str:
    stable_parts = (
        str(inbound.business_id),
        str(inbound.conversation_id),
        inbound.provider_message_id,
    )
    fingerprint = hashlib.sha256("\x1f".join(stable_parts).encode()).hexdigest()
    return f"conversation:outbound:{fingerprint}"

from __future__ import annotations

import hashlib
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.conversations.ports import BookingAvailabilityPort
from app.conversations.transitions import determine_transition
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
            if not conversation.automation_enabled:
                return False

            idempotency_key = build_outbound_idempotency_key(inbound)
            if await self.repository.outbound_exists(idempotency_key):
                return False

            transition = await determine_transition(
                conversation,
                inbound,
                self.booking_port,
            )
            if transition is None:
                return False
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

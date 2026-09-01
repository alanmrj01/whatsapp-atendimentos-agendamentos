from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.conversations.types import (
    ConversationSnapshot,
    ConversationTransition,
)
from app.models import Conversation, Message


def build_lock_conversation_statement(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Select[tuple[Conversation]]:
    return (
        select(Conversation)
        .where(
            Conversation.business_id == business_id,
            Conversation.id == conversation_id,
        )
        .with_for_update()
    )


def build_outbound_insert_statement(
    snapshot: ConversationSnapshot,
    transition: ConversationTransition,
    idempotency_key: str,
) -> Insert:
    outbound = transition.outbound
    return (
        postgresql_insert(Message)
        .values(
            id=uuid.uuid4(),
            business_id=snapshot.business_id,
            conversation_id=snapshot.conversation_id,
            provider_message_id=None,
            direction="outbound",
            message_type=outbound.message_type,
            body=outbound.body,
            interactive_id=outbound.interactive_id,
            status="pending",
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(
            index_elements=[Message.idempotency_key],
            index_where=Message.idempotency_key.is_not(None),
        )
        .returning(Message.id)
    )


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @asynccontextmanager
    async def lock_conversation(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AsyncIterator[ConversationSnapshot]:
        result = await self.session.execute(
            build_lock_conversation_statement(business_id, conversation_id)
        )
        conversation = result.scalar_one()
        yield ConversationSnapshot(
            business_id=conversation.business_id,
            customer_id=conversation.customer_id,
            conversation_id=conversation.id,
            state=conversation.state,
            context=dict(conversation.context),
            automation_enabled=conversation.automation_enabled,
            handoff_status=conversation.handoff_status,
        )

    async def outbound_exists(self, idempotency_key: str) -> bool:
        result = await self.session.execute(
            select(Message.id).where(Message.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none() is not None

    async def persist_transition(
        self,
        snapshot: ConversationSnapshot,
        transition: ConversationTransition,
        idempotency_key: str,
    ) -> bool:
        result = await self.session.execute(
            build_outbound_insert_statement(
                snapshot,
                transition,
                idempotency_key,
            )
        )
        if result.scalar_one_or_none() is None:
            return False

        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == snapshot.business_id,
                Conversation.id == snapshot.conversation_id,
            )
            .values(
                state=transition.state.value,
                context=transition.context,
                automation_enabled=transition.automation_enabled,
                handoff_status=transition.handoff_status,
            )
        )
        return True

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from contextlib import asynccontextmanager

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.conversations.constants import ConversationState
from app.conversations.types import (
    ConversationSnapshot,
    ConversationTransition,
)
from app.models import Business, Conversation, Customer, Message


def build_lock_conversation_statement(
    business_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> Select:
    return (
        select(
            Conversation,
            Business.assistant_enabled,
            Business.assistant_greeting_message,
            Business.assistant_fallback_message,
            Business.assistant_handoff_message,
            Business.timezone.label("business_timezone"),
            Customer.name.label("customer_name"),
            Customer.whatsapp_profile_name.label("whatsapp_profile_name"),
        )
        .join(Business, Business.id == Conversation.business_id)
        .join(
            Customer,
            and_(
                Customer.business_id == Conversation.business_id,
                Customer.id == Conversation.customer_id,
            ),
        )
        .where(
            Conversation.business_id == business_id,
            Conversation.id == conversation_id,
        )
        .with_for_update()
    )


def _sequence_group(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _message_payload(
    outbound,
    *,
    transition_state: ConversationState,
    sequence_group: str | None = None,
    sequence_index: int | None = None,
    sequence_count: int | None = None,
) -> dict | None:
    outbound_payload = deepcopy(outbound.outbound_payload)
    if transition_state is ConversationState.HUMAN_HANDOFF:
        outbound_payload = outbound_payload or {}
        outbound_payload["_alovia_transition"] = "handoff"
    if sequence_group is not None:
        outbound_payload = outbound_payload or {}
        outbound_payload["_alovia_sequence_group"] = sequence_group
        outbound_payload["_alovia_sequence_index"] = sequence_index
        outbound_payload["_alovia_sequence_count"] = sequence_count
    return outbound_payload


def _outbound_insert_statement(
    snapshot: ConversationSnapshot,
    transition: ConversationTransition,
    idempotency_key: str,
    outbound,
    *,
    sequence_group: str | None = None,
    sequence_index: int | None = None,
    sequence_count: int | None = None,
) -> Insert:
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
            outbound_payload=_message_payload(
                outbound,
                transition_state=transition.state,
                sequence_group=sequence_group,
                sequence_index=sequence_index,
                sequence_count=sequence_count,
            ),
            status="pending",
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(
            index_elements=[Message.idempotency_key],
            index_where=Message.idempotency_key.is_not(None),
        )
        .returning(Message.id)
    )


def build_outbound_insert_statement(
    snapshot: ConversationSnapshot,
    transition: ConversationTransition,
    idempotency_key: str,
) -> Insert:
    sequence_count = 1 + len(transition.follow_ups)
    group = _sequence_group(idempotency_key) if transition.follow_ups else None
    return _outbound_insert_statement(
        snapshot,
        transition,
        idempotency_key,
        transition.outbound,
        sequence_group=group,
        sequence_index=0 if group is not None else None,
        sequence_count=sequence_count if group is not None else None,
    )


def build_follow_up_insert_statement(
    snapshot: ConversationSnapshot,
    transition: ConversationTransition,
    idempotency_key: str,
    index: int,
) -> Insert:
    if index < 1 or index > len(transition.follow_ups):
        raise ValueError("Follow-up index is invalid")
    group = _sequence_group(idempotency_key)
    return _outbound_insert_statement(
        snapshot,
        transition,
        f"{idempotency_key}:followup:{index}",
        transition.follow_ups[index - 1],
        sequence_group=group,
        sequence_index=index,
        sequence_count=1 + len(transition.follow_ups),
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
        row = result.one()
        conversation = row[0]
        yield ConversationSnapshot(
            business_id=conversation.business_id,
            customer_id=conversation.customer_id,
            conversation_id=conversation.id,
            state=conversation.state,
            context=dict(conversation.context),
            automation_enabled=conversation.automation_enabled,
            handoff_status=conversation.handoff_status,
            assistant_enabled=row.assistant_enabled,
            greeting_message=row.assistant_greeting_message,
            fallback_message=row.assistant_fallback_message,
            handoff_message=row.assistant_handoff_message,
            customer_name=row.customer_name,
            whatsapp_profile_name=row.whatsapp_profile_name,
            business_timezone=row.business_timezone,
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

        for index in range(1, len(transition.follow_ups) + 1):
            await self.session.execute(
                build_follow_up_insert_statement(
                    snapshot,
                    transition,
                    idempotency_key,
                    index,
                )
            )

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
        if transition.customer_name is not None:
            await self.session.execute(
                update(Customer)
                .where(
                    Customer.business_id == snapshot.business_id,
                    Customer.id == snapshot.customer_id,
                )
                .values(name=transition.customer_name)
            )
        return True

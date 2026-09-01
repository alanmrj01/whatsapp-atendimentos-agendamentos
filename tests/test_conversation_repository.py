from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from app.conversations.constants import ConversationState
from app.conversations.outbound import main_menu_message
from app.conversations.types import (
    ConversationSnapshot,
    ConversationTransition,
)
from app.repositories.conversations import (
    build_lock_conversation_statement,
    build_outbound_insert_statement,
)

BUSINESS_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
CUSTOMER_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
CONVERSATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")


def snapshot() -> ConversationSnapshot:
    return ConversationSnapshot(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        state=ConversationState.START,
        context={},
        automation_enabled=True,
        handoff_status="none",
    )


def transition() -> ConversationTransition:
    return ConversationTransition(
        state=ConversationState.MENU,
        context={},
        automation_enabled=True,
        handoff_status="none",
        outbound=main_menu_message(),
    )


def test_conversation_lock_uses_postgresql_for_update() -> None:
    statement = build_lock_conversation_statement(BUSINESS_ID, CONVERSATION_ID)
    sql = str(statement.compile(dialect=postgresql_dialect()))

    assert "FROM conversations" in sql
    assert "conversations.business_id" in sql
    assert "conversations.id" in sql
    assert "FOR UPDATE" in sql


def test_outbox_insert_is_pending_sanitized_and_idempotent() -> None:
    idempotency_key = "conversation:outbound:deterministic-key"
    statement = build_outbound_insert_statement(
        snapshot(),
        transition(),
        idempotency_key,
    )
    compiled = statement.compile(dialect=postgresql_dialect())
    sql = str(compiled)
    parameters = compiled.params

    assert "INSERT INTO messages" in sql
    assert "ON CONFLICT (idempotency_key)" in sql
    assert "idempotency_key IS NOT NULL" in sql
    assert "DO NOTHING" in sql
    assert parameters["direction"] == "outbound"
    assert parameters["status"] == "pending"
    assert parameters["provider_message_id"] is None
    assert parameters["idempotency_key"] == idempotency_key
    assert parameters["message_type"] == "interactive_list"

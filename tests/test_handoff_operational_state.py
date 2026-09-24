from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.conversations.constants import ConversationState
from app.operations.schemas import ConversationAutomationUpdate
from app.operations.service import OperationalService, _conversation_view


BUSINESS_ID = uuid.UUID("b0000000-0000-0000-0000-000000000001")
CONVERSATION_ID = uuid.UUID("b0000000-0000-0000-0000-000000000002")
CUSTOMER_ID = uuid.UUID("b0000000-0000-0000-0000-000000000003")


@pytest.mark.asyncio
async def test_reenabling_handoff_resets_state_and_context() -> None:
    conversation = SimpleNamespace(
        state=ConversationState.HUMAN_HANDOFF.value,
        context={"service_id": "stale"},
        automation_enabled=False,
        handoff_status="waiting",
        automation_suppressed_until=None,
        suppression_reason=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=conversation),
        commit=AsyncMock(),
    )
    service = OperationalService(session)  # type: ignore[arg-type]
    expected = object()
    service.get_conversation = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await service.update_conversation_automation(
        BUSINESS_ID,
        CONVERSATION_ID,
        ConversationAutomationUpdate(enabled=True),
    )

    assert result is expected
    assert conversation.automation_enabled is True
    assert conversation.handoff_status == "none"
    assert conversation.state == ConversationState.START.value
    assert conversation.context == {}
    assert conversation.automation_suppressed_until is None
    assert conversation.suppression_reason is None
    session.commit.assert_awaited_once()


def test_pending_handoff_notification_is_waiting_not_in_progress() -> None:
    item = SimpleNamespace(
        id=CONVERSATION_ID,
        customer_id=CUSTOMER_ID,
        handoff_status="waiting",
        pinned_at=None,
        manual_unread=False,
    )
    row = (
        item,
        None,
        None,
        None,
        "5511999999999",
        "Seu atendimento foi encaminhado para uma pessoa da equipe.",
        datetime(2026, 9, 24, tzinfo=UTC),
        "outbound",
        "handoff",
        0,
    )

    view = _conversation_view(row)

    assert view.status == "waiting"
    assert view.priority is True


def test_manual_reply_after_handoff_is_in_progress() -> None:
    item = SimpleNamespace(
        id=CONVERSATION_ID,
        customer_id=CUSTOMER_ID,
        handoff_status="waiting",
        pinned_at=None,
        manual_unread=False,
    )
    row = (
        item,
        None,
        None,
        None,
        "5511999999999",
        "Olá, vou assumir seu atendimento.",
        datetime(2026, 9, 24, tzinfo=UTC),
        "outbound",
        None,
        0,
    )

    view = _conversation_view(row)

    assert view.status == "in_progress"

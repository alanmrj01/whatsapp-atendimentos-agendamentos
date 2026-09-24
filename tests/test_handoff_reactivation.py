from __future__ import annotations

import uuid

import pytest

from app.conversations.constants import ConversationState
from app.conversations.transitions import determine_transition
from app.conversations.types import ConversationInput, ConversationSnapshot


BUSINESS_ID = uuid.UUID("a0000000-0000-0000-0000-000000000001")
CUSTOMER_ID = uuid.UUID("a0000000-0000-0000-0000-000000000002")
CONVERSATION_ID = uuid.UUID("a0000000-0000-0000-0000-000000000003")


def snapshot(*, automation_enabled: bool) -> ConversationSnapshot:
    return ConversationSnapshot(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        state=ConversationState.HUMAN_HANDOFF.value,
        context={},
        automation_enabled=automation_enabled,
        handoff_status="none" if automation_enabled else "waiting",
        customer_name="Cliente",
        business_timezone="America/Sao_Paulo",
    )


def inbound(body: str = "Olá") -> ConversationInput:
    return ConversationInput(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        provider_message_id="wamid.test-reactivation",
        message_type="text",
        body=body,
        interactive_id=None,
        whatsapp_id="5511999999999",
    )


@pytest.mark.asyncio
async def test_reenabled_handoff_conversation_resumes_on_next_inbound() -> None:
    transition = await determine_transition(
        snapshot(automation_enabled=True),
        inbound(),
        booking_port=None,
    )

    assert transition is not None
    assert transition.state is ConversationState.MENU
    assert transition.automation_enabled is True
    assert transition.handoff_status == "none"
    assert transition.outbound.body is not None
    assert "Cliente" in transition.outbound.body
    assert transition.outbound.body.endswith("Como posso ajudá-lo?")


@pytest.mark.asyncio
async def test_disabled_handoff_conversation_remains_silent() -> None:
    transition = await determine_transition(
        snapshot(automation_enabled=False),
        inbound(),
        booking_port=None,
    )

    assert transition is None

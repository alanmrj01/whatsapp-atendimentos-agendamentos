"""Round 2F.2b baseline: stale equipment ownership buttons and reschedule alias."""

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


def test_trocar_o_horario_is_reschedule_intent() -> None:
    result = DeterministicConversationInterpreter().interpret("Quero trocar o horário")
    assert result.has(ConversationIntent.RESCHEDULE)


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("equipment.has", "Já tenho o aparelho"),
        ("equipment.needs", "Preciso de um aparelho"),
    ],
)
@pytest.mark.asyncio
async def test_stale_equipment_ownership_button_requires_confirmation(
    action: str,
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(2900, action=action, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["pending_change_action"] == action
    assert "equipment_ownership" not in repository.context
    assert repository.context["selected_date"] == "2026-09-02"
    assert "quer mudar" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

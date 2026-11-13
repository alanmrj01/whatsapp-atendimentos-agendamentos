"""Round 2F.2b baseline: stale equipment ownership buttons and reschedule alias."""

import pytest

from app.conversations.constants import CHANGE_CONFIRM, ConversationState
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


@pytest.mark.parametrize(
    ("action", "body", "expected_ownership"),
    [
        ("equipment.has", "Já tenho o aparelho", "has_equipment"),
        ("equipment.needs", "Preciso de um aparelho", "needs_equipment"),
    ],
)
@pytest.mark.asyncio
async def test_confirmed_stale_ownership_change_applies_and_invalidates_schedule(
    action: str,
    body: str,
    expected_ownership: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "equipment_ownership": (
                "needs_equipment" if expected_ownership == "has_equipment"
                else "has_equipment"
            ),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"date": "2026-09-02", "time": "09:00"},
        },
    )
    port = FakeBookingPort()
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(2910, action=action, body=body))
    await engine.process(
        inbound(2911, action=CHANGE_CONFIRM, body="Confirmar mudança")
    )

    assert repository.context["equipment_ownership"] == expected_ownership
    assert "pending_change_action" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "confirm" not in port.calls
    assert repository.state != ConversationState.HUMAN_HANDOFF

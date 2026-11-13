"""Round 2D.2.2: duration questions are not booking-time answers."""

from unittest.mock import AsyncMock

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


DURATION_CASES = (
    ("o serviço demora 2 horas?", "02:00"),
    ("leva 2h?", "02:00"),
    ("esse atendimento demora 9 horas?", "09:00"),
    ("o serviço pode levar 3 horas?", "03:00"),
    ("a instalação dura 4 horas?", "04:00"),
    ("vocês levam umas 2h para terminar?", "02:00"),
    ("esse serviço de limpeza demora 2 horas?", "02:00"),
)


def _time_scenario(
    slots: list[str],
) -> tuple[FakeConversationRepository, FakeBookingPort, ConversationEngine]:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-10-14",
            "service_address": "Rua Segura, 10, Centro",
            "quantity": 1,
        },
        customer_name="Cliente Teste",
    )
    port = FakeBookingPort()
    options = tuple(BookingOption(slot, slot) for slot in slots)
    port.times = list(options)
    port.list_times = AsyncMock(return_value=options)
    return repository, port, ConversationEngine(repository, port)


@pytest.mark.parametrize("body", [body for body, _ in DURATION_CASES])
def test_duration_language_has_side_question_semantics(body: str) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(ConversationIntent.DURATION_QUESTION)
    assert interpretation.has_act(ConversationAct.SIDE_QUESTION)


@pytest.mark.parametrize(("body", "matching_slot"), DURATION_CASES)
@pytest.mark.asyncio
async def test_duration_question_never_selects_matching_offered_time(
    body: str,
    matching_slot: str,
) -> None:
    repository, port, engine = _time_scenario(
        [matching_slot, "14:30", "18:00"]
    )

    await engine.process(inbound(2500, body=body))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert repository.context["service_address"] == "Rua Segura, 10, Centro"
    assert repository.context["quantity"] == 1
    assert "selected_time" not in repository.context
    assert "repair_attempts" not in repository.context
    assert port.confirmations == []
    assert "30 minutos" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@pytest.mark.parametrize(
    ("body", "expected", "slots"),
    [
        ("2", "02:00", ["02:00", "14:00"]),
        ("02", "02:00", ["02:00", "14:00"]),
        ("2h", "02:00", ["02:00", "14:00"]),
        ("2 horas", "02:00", ["02:00", "14:00"]),
        ("02:00", "02:00", ["02:00", "14:00"]),
        ("às 2", "02:00", ["02:00", "14:00"]),
        ("duas horas", "02:00", ["02:00", "14:00"]),
        ("9", "09:00", ["09:00", "14:00"]),
        ("9h", "09:00", ["09:00", "14:00"]),
        ("9 horas", "09:00", ["09:00", "14:00"]),
        ("às 9", "09:00", ["09:00", "14:00"]),
        ("duas da tarde", "14:00", ["02:00", "14:00"]),
        ("duas e meia da tarde", "14:30", ["02:30", "14:30"]),
        ("14:30", "14:30", ["09:00", "14:30"]),
        ("meio-dia", "12:00", ["09:00", "12:00"]),
        ("meia-noite", "00:00", ["00:00", "09:00"]),
    ],
)
@pytest.mark.asyncio
async def test_legitimate_time_answer_still_selects_only_an_offered_slot(
    body: str,
    expected: str,
    slots: list[str],
) -> None:
    repository, port, engine = _time_scenario(slots)

    await engine.process(inbound(2510, body=body))

    assert repository.state != ConversationState.BOOKING_TIME
    assert repository.context["selected_time"] == expected
    assert repository.context["selected_date"] == "2026-10-14"
    assert port.confirmations == []

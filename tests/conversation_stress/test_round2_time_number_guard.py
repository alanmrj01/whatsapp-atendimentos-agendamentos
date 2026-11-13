"""Round 2D.2.1: incidental numbers are not booking-time answers."""

from unittest.mock import AsyncMock

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
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


@pytest.mark.parametrize(
    ("body", "matching_slot"),
    [
        ("essa visita tem garantia de 2 anos?", "02:00"),
        ("esse aparelho é de 9 mil BTUs?", "09:00"),
        ("o serviço leva 14 minutos?", "14:00"),
        ("são 3 aparelhos mesmo?", "03:00"),
        ("posso parcelar em 10 vezes?", "10:00"),
        ("tem 5 metros de tubulação?", "05:00"),
        ("o endereço é número 9?", "09:00"),
    ],
)
@pytest.mark.asyncio
async def test_incidental_number_never_selects_matching_offered_time(
    body: str,
    matching_slot: str,
) -> None:
    repository, port, engine = _time_scenario(
        [matching_slot, "16:00", "18:30"]
    )

    await engine.process(inbound(2400, body=body))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert repository.context["service_address"] == "Rua Segura, 10, Centro"
    assert repository.context["quantity"] == 1
    assert "selected_time" not in repository.context
    assert "repair_attempts" not in repository.context
    assert port.confirmations == []


@pytest.mark.parametrize(
    ("body", "expected", "slots"),
    [
        ("9", "09:00", ["09:00", "14:00"]),
        ("09", "09:00", ["09:00", "14:00"]),
        ("14", "14:00", ["09:00", "14:00"]),
        ("9h", "09:00", ["09:00", "14:00"]),
        ("09:00", "09:00", ["09:00", "14:00"]),
        ("às 9", "09:00", ["09:00", "14:00"]),
        ("9 horas", "09:00", ["09:00", "14:00"]),
        ("pode ser nove horas", "09:00", ["09:00", "14:00"]),
        ("14h30", "14:30", ["09:00", "14:30"]),
        ("14:30", "14:30", ["09:00", "14:30"]),
        ("duas da tarde", "14:00", ["02:00", "14:00"]),
        ("duas e meia da tarde", "14:30", ["02:30", "14:30"]),
        ("duas e meia", "14:30", ["09:00", "14:30"]),
        ("meio-dia", "12:00", ["09:00", "12:00"]),
        ("meia-noite", "00:00", ["00:00", "09:00"]),
    ],
)
@pytest.mark.asyncio
async def test_compact_or_explicit_time_still_selects_only_an_offered_slot(
    body: str,
    expected: str,
    slots: list[str],
) -> None:
    repository, port, engine = _time_scenario(slots)

    await engine.process(inbound(2410, body=body))

    assert repository.state != ConversationState.BOOKING_TIME
    assert repository.context["selected_time"] == expected
    assert repository.context["selected_date"] == "2026-10-14"
    assert port.confirmations == []


@pytest.mark.asyncio
async def test_bare_number_keeps_existing_exact_24_hour_rule() -> None:
    repository, port, engine = _time_scenario(["02:00", "14:00"])

    await engine.process(inbound(2420, body="2"))

    assert repository.state != ConversationState.BOOKING_TIME
    assert repository.context["selected_time"] == "02:00"
    assert port.confirmations == []

"""Round 2D.1.1: an appointment question is not a property restriction."""

from dataclasses import replace

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


SERVICE_ADDRESS = {
    "address_line": "Rua das Flores, 10, Centro",
    "city": "São José dos Campos",
    "state": "SP",
}


def _scenario() -> tuple[FakeConversationRepository, ConversationEngine]:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SITE_LIMIT,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "property_type": "house",
            "service_address": SERVICE_ADDRESS,
            "request_mode": "booking",
        },
        customer_name="Cliente Teste",
    )
    booking = FakeBookingPort()
    booking.intake = replace(booking.intake, asks_site_time_limit=True)
    return repository, ConversationEngine(repository, booking)


@pytest.mark.parametrize(
    "body",
    [
        "tem horário até 16h?",
        "tem vaga até 17h?",
        "vocês têm algum horário até 15h?",
        "consegue vir até 16h?",
        "qual horário vocês têm até 18h?",
        "tem disponibilidade até 14h?",
    ],
    ids=[
        "has-time",
        "has-vacancy",
        "has-any-time",
        "can-come",
        "which-time",
        "has-availability",
    ],
)
@pytest.mark.asyncio
async def test_availability_question_never_becomes_property_site_limit(
    body: str,
) -> None:
    repository, engine = _scenario()
    expected_context = dict(repository.context)

    await engine.process(inbound(2200, body=body))

    assert repository.state == ConversationState.BOOKING_SITE_LIMIT
    assert repository.context == expected_context
    assert "site_limit_answered" not in repository.context
    assert "site_allowed_end" not in repository.context
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("condomínio permite serviço até 17h", "17:00"),
        ("o limite é 18:00", "18:00"),
        ("não pode depois das 16h", "16:00"),
        ("serviço só até 15h", "15:00"),
        ("a portaria só permite entrada até 14h", "14:00"),
        ("o prédio permite técnico até 17h", "17:00"),
    ],
    ids=[
        "condominium",
        "explicit-limit",
        "not-after",
        "service-only-until",
        "gate-entry",
        "building-technician",
    ],
)
@pytest.mark.asyncio
async def test_explicit_property_restriction_remains_a_valid_site_limit(
    body: str,
    expected: str,
) -> None:
    repository, engine = _scenario()

    await engine.process(inbound(2210, body=body))

    assert repository.context["site_limit_answered"] is True
    assert repository.context["site_allowed_end"] == expected
    assert repository.context["quantity"] == 2
    assert repository.context["service_address"] == SERVICE_ADDRESS
    assert repository.state == ConversationState.BOOKING_DATE


@pytest.mark.asyncio
async def test_compact_answer_remains_valid_at_site_limit_checkpoint() -> None:
    repository, engine = _scenario()

    await engine.process(inbound(2220, body="até 17h"))

    assert repository.context["site_limit_answered"] is True
    assert repository.context["site_allowed_end"] == "17:00"
    assert repository.state == ConversationState.BOOKING_DATE

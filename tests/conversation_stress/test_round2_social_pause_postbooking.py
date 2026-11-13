"""Round 2F.2a baseline: natural post-booking close, social acknowledgements and pauses."""

from dataclasses import replace

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    confirmation_context,
    inbound,
)


FAILING_SOCIAL = (
    "valeu viu",
    "show de bola",
    "beleza então",
    "perfeito, obrigado",
    "ok, entendido",
    "tmj",
    "top demais",
    "tranquilo então",
    "boa, obrigado",
    "fechou",
)


def _address_scenario():
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    port = FakeBookingPort()
    port.intake = replace(port.intake, requires_address=True)
    return repository, port, ConversationEngine(repository, port)


@pytest.mark.parametrize("body", FAILING_SOCIAL)
@pytest.mark.asyncio
async def test_social_acknowledgement_preserves_checkpoint_without_retry(body: str) -> None:
    repository, port, engine = _address_scenario()
    before = dict(repository.context)

    await engine.process(inbound(2800, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context == before
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context


@pytest.mark.parametrize("body", ("valeu viu", "show de bola", "tmj", "fechou então"))
@pytest.mark.asyncio
async def test_repeated_social_acknowledgement_never_forces_handoff(body: str) -> None:
    repository, port, engine = _address_scenario()

    await engine.process(inbound(2810, body=body))
    await engine.process(inbound(2811, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context


@pytest.mark.parametrize(
    "body",
    ("pera aí um minutinho", "já volto", "deixa eu ver aqui"),
)
@pytest.mark.asyncio
async def test_short_pause_preserves_checkpoint_without_retry(body: str) -> None:
    repository, port, engine = _address_scenario()
    before = dict(repository.context)

    await engine.process(inbound(2820, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context == before
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context


@pytest.mark.asyncio
async def test_natural_post_booking_decline_completes_cleanly() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context=confirmation_context(),
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(2830, body="Não, obrigado. Era só isso")
    )

    assert repository.state == ConversationState.COMPLETED
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "cancel_booking" not in port.calls
    assert "reschedule_booking" not in port.calls

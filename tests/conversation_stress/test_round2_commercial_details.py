"""Round 2F.1 baseline: structured tubing commercial details."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


CHECKPOINTS = (
    pytest.param(
        ConversationState.BOOKING_ADDRESS,
        {"service_id": str(SERVICE_ID)},
        "endereço",
        id="address",
    ),
    pytest.param(
        ConversationState.BOOKING_DATE,
        {"service_id": str(SERVICE_ID)},
        "escolha uma data",
        id="date",
    ),
    pytest.param(
        ConversationState.BOOKING_TIME,
        {"service_id": str(SERVICE_ID), "selected_date": "2026-09-02"},
        "09:00",
        id="time",
    ),
)


@pytest.mark.parametrize(("state", "context", "resume_fragment"), CHECKPOINTS)
@pytest.mark.parametrize(
    ("body", "answer_fragment"),
    [
        ("A tubulação está inclusa?", "3 metros"),
        ("Quanto custa cada metro adicional?", "r$ 20,00"),
    ],
)
@pytest.mark.asyncio
async def test_structured_tubing_fact_answers_and_resumes_checkpoint(
    state: ConversationState,
    context: dict[str, object],
    resume_fragment: str,
    body: str,
    answer_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    booking_port.extra_tubing_price = Decimal("20.00")
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=state is ConversationState.BOOKING_ADDRESS,
    )
    before = dict(repository.context)

    await ConversationEngine(repository, booking_port).process(inbound(2700, body=body))

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state == state
    assert repository.context == before
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert answer_fragment in response
    assert resume_fragment.casefold() in response
    if "metro adicional" in body.casefold():
        assert "r$ 100,00" not in response

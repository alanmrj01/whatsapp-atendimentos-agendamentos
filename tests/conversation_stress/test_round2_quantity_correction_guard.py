"""Rodada 2B.1 — contenção de falsa correção tardia de quantidade."""

from __future__ import annotations

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("na verdade são 3 aparelhos", 3),
        ("corrigindo: preciso de quatro unidades", 4),
        ("não são 2, são cinco aparelhos", 5),
        ("quis dizer 3 aparelhos", 3),
    ],
)
@pytest.mark.asyncio
async def test_explicit_quantity_correction_still_updates_and_invalidates_dependents(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "equipment_quantity": 2,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"selected_time": "09:00"},
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(301, body=body)
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.parametrize(
    "body",
    [
        "quanto custa para 2 aparelhos?",
        "e se forem 3 aparelhos, fica quanto?",
        "se eu tiver 4 unidades muda o preço?",
        "vocês atendem 2 aparelhos?",
        "para 3 aparelhos o valor muda?",
    ],
)
@pytest.mark.asyncio
async def test_quantity_in_question_does_not_mutate_or_invalidate_booking(
    body: str,
) -> None:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={
            **candidate,
            "quantity": 1,
            "candidate_booking": candidate,
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(302, body=body)
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context["quantity"] == 1
    assert "equipment_quantity" not in repository.context
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == "09:00"
    assert repository.context["candidate_booking"] == candidate
    assert repository.context["quote_presented"] is True
    assert repository.state != ConversationState.BOOKING_DATE

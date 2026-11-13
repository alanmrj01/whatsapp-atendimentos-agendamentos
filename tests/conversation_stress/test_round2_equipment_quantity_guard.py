"""Rodada 2C.1 — quantidade exige vínculo semântico com equipamento."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.facts import enrich_context_from_message
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


INCIDENTAL_EQUIPMENT_NUMBERS = (
    "meu aparelho é de 12 mil BTUs?",
    "o aparelho tem garantia de 2 anos?",
    "esse split demora 3 horas para instalar?",
    "o equipamento fica no número 25 da rua",
    "esse aparelho pode parcelar em 10 vezes?",
    "o split usa 3 metros de tubulação?",
)

EXPLICIT_EQUIPMENT_COUNTS = (
    ("2 aparelhos", 2),
    ("1 aparelho", 1),
    ("3 splits", 3),
    ("quatro unidades", 4),
    ("tenho cinco aparelhos", 5),
    ("preciso limpar uma unidade", 1),
    ("são 3 aparelhos", 3),
)


def _quantity_port() -> FakeBookingPort:
    port = FakeBookingPort()
    port.intake = replace(port.intake, requires_quantity=True)
    return port


@pytest.mark.parametrize("body", INCIDENTAL_EQUIPMENT_NUMBERS)
def test_global_enrichment_rejects_numbers_with_another_semantic_role(
    body: str,
) -> None:
    enriched = enrich_context_from_message({}, body)

    assert "quantity" not in enriched
    assert "equipment_quantity" not in enriched


@pytest.mark.parametrize(("body", "expected"), EXPLICIT_EQUIPMENT_COUNTS)
def test_global_enrichment_accepts_explicit_equipment_count(
    body: str,
    expected: int,
) -> None:
    enriched = enrich_context_from_message({}, body)

    assert enriched["quantity"] == expected
    assert enriched["equipment_quantity"] == expected


@pytest.mark.parametrize("body", ["1", "2", "dois", "quatro"])
def test_global_enrichment_never_promotes_bare_number(body: str) -> None:
    enriched = enrich_context_from_message({}, body)

    assert "quantity" not in enriched
    assert "equipment_quantity" not in enriched


def test_global_enrichment_uses_locally_bound_count_when_message_has_two_numbers() -> None:
    enriched = enrich_context_from_message(
        {},
        "meu aparelho é de 12 mil BTUs e tenho 2 aparelhos",
    )

    assert enriched["quantity"] == 2
    assert enriched["equipment_quantity"] == 2


@pytest.mark.parametrize("body", INCIDENTAL_EQUIPMENT_NUMBERS)
def test_global_enrichment_does_not_replace_confirmed_quantity(body: str) -> None:
    context = {"quantity": 2, "equipment_quantity": 2}

    enriched = enrich_context_from_message(context, body)

    assert enriched == context


@pytest.mark.parametrize("body", INCIDENTAL_EQUIPMENT_NUMBERS)
@pytest.mark.asyncio
async def test_quantity_checkpoint_does_not_consume_incidental_equipment_number(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
        },
    )

    await ConversationEngine(repository, _quantity_port()).process(
        inbound(201, body=body)
    )

    assert repository.state == ConversationState.BOOKING_QUANTITY
    assert "quantity" not in repository.context
    assert "equipment_quantity" not in repository.context


@pytest.mark.parametrize(
    ("body", "expected"),
    (*EXPLICIT_EQUIPMENT_COUNTS, ("1", 1), ("2", 2), ("dois", 2), ("quatro", 4)),
)
@pytest.mark.asyncio
async def test_quantity_checkpoint_accepts_explicit_or_compact_count(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
        },
    )

    await ConversationEngine(repository, _quantity_port()).process(
        inbound(202, body=body)
    )

    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert repository.state != ConversationState.BOOKING_QUANTITY


@pytest.mark.asyncio
async def test_quantity_checkpoint_uses_equipment_count_not_earlier_btu_number() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
        },
    )

    await ConversationEngine(repository, _quantity_port()).process(
        inbound(
            204,
            body="meu aparelho é de 12 mil BTUs e tenho 2 aparelhos",
        )
    )

    assert repository.context["quantity"] == 2
    assert repository.context["equipment_quantity"] == 2


@pytest.mark.asyncio
async def test_incidental_number_preserves_confirmed_quantity_and_schedule() -> None:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    context = {
        **candidate,
        "quantity": 2,
        "equipment_quantity": 2,
        "candidate_booking": candidate,
        "quote_presented": True,
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=context,
    )

    await ConversationEngine(repository, _quantity_port()).process(
        inbound(203, body="o aparelho tem garantia de 3 anos?")
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context["quantity"] == 2
    assert repository.context["equipment_quantity"] == 2
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == "09:00"
    assert repository.context["candidate_booking"] == candidate
    assert repository.context["quote_presented"] is True

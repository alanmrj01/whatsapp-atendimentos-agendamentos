"""Round 2E baseline: service semantics, HVAC boundaries and custom catalog."""

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from app.conversations.ports import BookingOption
from app.conversations.service_semantics import generate_service_intent_examples
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


@pytest.mark.parametrize(
    ("body", "service_key"),
    [
        ("fala mano, meu ar n gela", "diagnostics"),
        ("oxe, o split ta pingano", "diagnostics"),
        ("uai, meu ar tá fazendo um barui estranho", "diagnostics"),
        ("ar n liga", "diagnostics"),
        ("qro limpa o ar", "cleaning"),
        ("preciso instlar um split", "split-installation"),
        ("limpesa no ar condisionado", "cleaning"),
        ("higienizassao do split", "cleaning"),
        ("meu ar tá estralando", "diagnostics"),
        ("meu ar tá só ventilando", "diagnostics"),
    ],
)
def test_real_whatsapp_hvac_language_resolves_service(body: str, service_key: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)
    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key == service_key


@pytest.mark.parametrize(
    "body",
    [
        "vcs vende ar?",
        "gostaria de cotar um ar novo para o quarto",
    ],
)
def test_equipment_purchase_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)
    assert result.has(ConversationIntent.EQUIPMENT_PURCHASE)
    assert not result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key is None


@pytest.mark.parametrize(
    "body",
    [
        "preciso limpar o sofá",
        "quero instalar uma televisão na sala",
        "preciso de carga de gás no carro",
        "o chuveiro está pingando",
    ],
)
def test_unrelated_language_does_not_open_hvac_service(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)
    assert not result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key is None


@pytest.mark.asyncio
async def test_configured_custom_catalog_service_is_resolved() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Higienização de evaporadora industrial",
            generate_service_intent_examples("Higienização de evaporadora industrial"),
        ),
        BookingOption(
            "40000000-0000-0000-0000-000000000099",
            "Limpeza de ar-condicionado split",
            generate_service_intent_examples("Limpeza de ar-condicionado split"),
        ),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(2600, body="quero higienização da evaporadora industrial")
    )

    assert repository.context.get("service_id") == str(SERVICE_ID)
    assert repository.state not in {
        ConversationState.BOOKING_SERVICE,
        ConversationState.HUMAN_HANDOFF,
    }

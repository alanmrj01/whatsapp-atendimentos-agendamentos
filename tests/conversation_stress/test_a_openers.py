"""Rodada 1/A: descoberta em START e BOOKING_SERVICE.

As expectativas abaixo derivam do contrato de produto, não do comportamento
atual. Falhas são evidências da rodada de descoberta e não autorizam mudança
em ``app/**``.
"""

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
        ("e aí parça, o ar parou", "diagnostics"),
        ("oxe, o split tá pingando", "diagnostics"),
        ("oxe, o split ta pingano", "diagnostics"),
        ("uai, meu ar tá fazendo um barui estranho", "diagnostics"),
        ("blz, preciso de higienização", "cleaning"),
        ("MEU AR NÃO RESFRIA 😓", "diagnostics"),
        ("ar n liga", "diagnostics"),
        ("qro limpa o ar", "cleaning"),
        ("preciso instlar um split", "split-installation"),
        ("bom dia, quero gás", "gas-recharge"),
        ("👋 preciso de limpeza", "cleaning"),
        ("limpesa no ar condisionado", "cleaning"),
        ("higienizassao do split", "cleaning"),
        ("meu ar tá estralando", "diagnostics"),
        ("meu ar tá só ventilando", "diagnostics"),
        ("meu ar está vazando água", "diagnostics"),
        ("preciso de PMOC", "pmoc"),
        ("quero manutenção preventiva do ar", "preventive-maintenance"),
        ("preciso fazer carga de gás no split", "gas-recharge"),
    ],
)
def test_clear_real_whatsapp_openers_resolve_service(
    body: str, service_key: str
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key == service_key


@pytest.mark.parametrize(
    ("body", "intent", "service_key"),
    [
        ("quero compra um split", ConversationIntent.EQUIPMENT_PURCHASE, None),
        ("vcs vende ar?", ConversationIntent.EQUIPMENT_PURCHASE, None),
        (
            "quanto custa um aparelho de ar condicionado?",
            ConversationIntent.EQUIPMENT_PURCHASE,
            None,
        ),
        (
            "gostaria de cotar um ar novo para o quarto",
            ConversationIntent.EQUIPMENT_PURCHASE,
            None,
        ),
        (
            "quanto custa instalar um aparelho?",
            ConversationIntent.SERVICE_INTENT,
            "split-installation",
        ),
        (
            "instalar equipamento que já comprei",
            ConversationIntent.SERVICE_INTENT,
            "split-installation",
        ),
        (
            "só preciso da instalação, o aparelho eu já tenho",
            ConversationIntent.SERVICE_INTENT,
            "split-installation",
        ),
        (
            "não quero comprar, só instalar",
            ConversationIntent.SERVICE_INTENT,
            "split-installation",
        ),
    ],
)
def test_purchase_and_installation_remain_distinct(
    body: str,
    intent: ConversationIntent,
    service_key: str | None,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(intent)
    assert result.service_key == service_key
    if intent is ConversationIntent.EQUIPMENT_PURCHASE:
        assert not result.has(ConversationIntent.SERVICE_INTENT)


@pytest.mark.parametrize(
    "body",
    [
        "preciso limpar o sofá",
        "quero instalar uma televisão na sala",
        "preciso de carga de gás no carro",
        "o chuveiro está pingando",
        "quanto custa a manutenção do carro?",
        "preciso instalar o aplicativo no celular",
        "o gás do fogão acabou",
    ],
)
def test_unrelated_language_does_not_open_refrigeration_service(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert not result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "service_name"),
    [
        ("fala mano, preciso limpar meu ar", "Limpeza e higienização"),
        ("oxe, meu split não está gelando", "Diagnóstico e manutenção corretiva"),
        ("uai, preciso fazer carga de gás no ar", "Carga de gás e teste de vazamento"),
        ("e aí brother, meu ar parou", "Diagnóstico e manutenção corretiva"),
    ],
)
async def test_informal_openers_do_not_force_handoff_or_caricature_tone(
    body: str, service_name: str
) -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), service_name),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body=body)
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state != ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert not any(
        slang in response
        for slang in ("mano", "brother", "parça", "véi", "vei", "tamo junto")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_name", "body"),
    [
        (
            "Manutenção de câmara fria comercial",
            "preciso de manutenção na minha câmara fria comercial",
        ),
        (
            "Higienização de evaporadora industrial",
            "quero higienização da evaporadora industrial",
        ),
    ],
)
async def test_configured_custom_catalog_service_is_resolved(
    service_name: str, body: str
) -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            service_name,
            generate_service_intent_examples(service_name),
        ),
        BookingOption(
            "40000000-0000-0000-0000-000000000099",
            "Limpeza de ar-condicionado split",
            generate_service_intent_examples("Limpeza de ar-condicionado split"),
        ),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body=body)
    )

    assert repository.context.get("service_id") == str(SERVICE_ID)
    assert repository.state not in {
        ConversationState.BOOKING_SERVICE,
        ConversationState.HUMAN_HANDOFF,
    }


@pytest.mark.asyncio
async def test_regionalism_is_not_saved_as_customer_location() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="oxe, preciso fazer uma limpeza no ar")
    )

    assert repository.context.get("service_id") == str(SERVICE_ID)
    assert "service_city" not in repository.context
    assert "service_address" not in repository.context
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.asyncio
async def test_clear_service_intent_at_service_checkpoint_does_not_restart_menu() -> None:
    repository = FakeConversationRepository(state=ConversationState.BOOKING_SERVICE)
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="beleza, quero higienizar meu split")
    )

    assert repository.context.get("service_id") == str(SERVICE_ID)
    assert repository.state not in {
        ConversationState.MENU,
        ConversationState.BOOKING_SERVICE,
        ConversationState.HUMAN_HANDOFF,
    }

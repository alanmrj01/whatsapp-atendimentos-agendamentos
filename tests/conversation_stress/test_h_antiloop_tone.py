from __future__ import annotations

from dataclasses import replace

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


@pytest.mark.parametrize(
    "body",
    [
        "obrigado",
        "valeu",
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
    ],
)
@pytest.mark.asyncio
async def test_social_message_does_not_spend_active_slot_retry(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(100, body=body)
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context


@pytest.mark.parametrize(
    "body",
    [
        "valeu viu",
        "show de bola",
        "tmj",
        "fechou então",
    ],
)
@pytest.mark.asyncio
async def test_repeated_social_message_never_forces_handoff(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(200, body=body))
    await engine.process(inbound(201, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("quero resolver um negócio", "é outra coisa"),
        ("preciso de uma ajuda diferente", "não é isso"),
        ("tenho um assunto", "um outro assunto"),
        ("quero ver uma coisa", "deixa pra lá, é outra coisa"),
    ],
)
@pytest.mark.asyncio
async def test_two_genuinely_unknown_openers_have_bounded_fallback(
    first: str,
    second: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.START,
        customer_name="Alan",
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(inbound(300, body=first))
    assert repository.state == ConversationState.MENU
    assert repository.context.get("fallback_variant") == 1

    await engine.process(inbound(301, body=second))

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"


@pytest.mark.parametrize("message_type", ["audio", "video", "image"])
@pytest.mark.asyncio
async def test_known_unsupported_media_offers_safe_text_or_handoff_choice(
    message_type: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(400, message_type=message_type)
    )

    outbound = repository.outbounds[-1].transition.outbound
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert outbound.interactive_id == "media.unsupported"
    assert "posso encaminhar" in (outbound.body or "").casefold()
    assert "não consigo analisar" not in (outbound.body or "").casefold()


@pytest.mark.parametrize(
    ("state", "context", "body"),
    [
        (
            ConversationState.BOOKING_ADDRESS,
            {"service_id": str(SERVICE_ID)},
            "Rua A, 10, Centro, São José dos Campos - SP",
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "02/09",
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
            "09:00",
        ),
        (
            ConversationState.START,
            {},
            "quero fazer limpeza no ar condicionado",
        ),
    ],
)
@pytest.mark.asyncio
async def test_duplicate_provider_event_never_creates_second_outbound(
    state: ConversationState,
    context: dict[str, str],
    body: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=state is ConversationState.BOOKING_ADDRESS,
    )
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    engine = ConversationEngine(repository, booking_port)
    event = inbound(500, body=body)

    assert await engine.process(event) is True
    assert await engine.process(event) is False
    assert len(repository.outbounds) == 1


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("property.house", "Casa"),
        ("quantity:2", "2 aparelhos"),
        ("access.difficult", "Acesso difícil"),
        ("equipment.has", "Já tenho o aparelho"),
    ],
)
@pytest.mark.asyncio
async def test_stale_button_requires_confirmation_before_changing_context(
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
        inbound(600, action=action, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["pending_change_action"] == action
    assert "quer mudar" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@pytest.mark.parametrize(
    ("body", "forbidden"),
    [
        ("oxe, quero limpar meu ar", "oxe"),
        ("uai, preciso de manutenção", "uai"),
        ("fala mano, meu ar não gela", "mano"),
        ("e aí parça, quero instalar um split", "parça"),
        ("bora ver esse ar que tá ruim", "bora"),
        ("véi, o aparelho tá pingando", "véi"),
    ],
)
@pytest.mark.asyncio
async def test_informal_language_is_understood_without_caricature(
    body: str,
    forbidden: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.START,
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização"),
        BookingOption(str(SERVICE_ID), "Diagnóstico e manutenção corretiva"),
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split"),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(700, body=body)
    )

    outbound = repository.outbounds[-1].transition.outbound.body or ""
    assert forbidden not in outbound.casefold()
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    "body",
    [
        "pera aí um minutinho",
        "já volto",
        "deixa eu ver aqui",
    ],
)
@pytest.mark.asyncio
async def test_short_interruption_preserves_slot_without_spending_retry(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(800, body=body)
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "repair_attempts" not in repository.context

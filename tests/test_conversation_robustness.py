from dataclasses import replace

import pytest

from app.booking.domain import PricingType
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


@pytest.mark.parametrize(
    ("body", "intent", "service_key"),
    [
        (
            "e aí, meu ar tá pingando demais",
            ConversationIntent.SERVICE_INTENT,
            "diagnostics",
        ),
        (
            "fala meu amigo, preciso instalar um split",
            ConversationIntent.SERVICE_INTENT,
            "split-installation",
        ),
        (
            "beleza, queria fazer uma limpeza no ar",
            ConversationIntent.SERVICE_INTENT,
            "cleaning",
        ),
        (
            "oxe, preciso colocar gás no ar",
            ConversationIntent.SERVICE_INTENT,
            "gas-recharge",
        ),
        (
            "uai, tem horário amanhã?",
            ConversationIntent.AVAILABILITY,
            None,
        ),
    ],
)
def test_real_whatsapp_openers_converge_to_existing_semantics(
    body: str,
    intent: ConversationIntent,
    service_key: str | None,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(intent)
    assert interpretation.service_key == service_key


@pytest.mark.parametrize(
    ("body", "service_key"),
    [
        ("não é limpeza, é manutenção", "diagnostics"),
        ("não quero comprar, só instalar", "split-installation"),
    ],
)
def test_explicit_correction_wins_without_destructive_guessing(
    body: str,
    service_key: str,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.service_key == service_key
    assert interpretation.has_act(ConversationAct.CORRECTION)


@pytest.mark.asyncio
async def test_informal_customer_language_does_not_force_informal_company_voice() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="fala mano, preciso limpar meu ar")
    )

    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state != ConversationState.HUMAN_HANDOFF
    assert not any(
        slang in body
        for slang in ("mano", "brother", "parça", "vei", "véi", "tamo junto")
    )


@pytest.mark.asyncio
async def test_side_price_question_keeps_address_checkpoint_and_does_not_become_address() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        pricing_type=PricingType.ESTIMATED,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="beleza, mas quanto custa esse serviço?")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "service_address" not in repository.context
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "r$ 100,00" in body
    assert "endereço" in body


@pytest.mark.asyncio
async def test_side_duration_question_keeps_time_checkpoint_and_resumes_available_time() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="rapidinho: quanto tempo demora?")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "30 minutos" in body
    assert "09:00" in body


@pytest.mark.asyncio
async def test_duplicate_customer_message_never_creates_duplicate_outbound() -> None:
    repository = FakeConversationRepository()
    engine = ConversationEngine(repository, FakeBookingPort())
    message = inbound(1, body="bom dia")

    first = await engine.process(message)
    second = await engine.process(message)

    assert first is True
    assert second is False
    assert len(repository.outbounds) == 1

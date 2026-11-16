"""Rodada 3 — orquestração conversacional contextual e recuperação profissional."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.booking.domain import PricingType
from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
    extract_customer_name,
)
from app.conversations.orchestration import (
    TurnRelation,
    classify_pending_turn,
    is_referral_message,
)
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


def _combined_body(repository: FakeConversationRepository) -> str:
    transition = repository.outbounds[-1].transition
    return " ".join(
        [
            transition.outbound.body or "",
            *(message.body or "" for message in transition.follow_ups),
        ]
    ).casefold()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("pode ser joao", "Joao"),
        ("me chama de joão", "João"),
        ("pode me chamar Alan", "Alan"),
        ("sou o joao", "Joao"),
        ("joao mesmo", "Joao"),
    ],
)
def test_pending_name_accepts_natural_conversational_replies(
    body: str,
    expected: str,
) -> None:
    assert extract_customer_name(body, allow_bare=True) == expected


@pytest.mark.parametrize(
    "body",
    [
        "pode ser amanhã",
        "pode ser hoje",
        "sou técnico",
        "me chama de atendimento",
    ],
)
def test_pending_name_never_promotes_operational_words_to_name(body: str) -> None:
    assert extract_customer_name(body, allow_bare=True) is None


@pytest.mark.parametrize(
    "body",
    [
        "peguei seu número com meu irmão",
        "minha irmã me indicou vocês",
        "o síndico me passou seu contato",
        "um amigo recomendou vocês",
        "meu vizinho falou de vocês",
    ],
)
def test_referral_language_is_recognized_without_fallback(body: str) -> None:
    assert is_referral_message(body) is True


def test_side_question_never_consumes_repair_attempt() -> None:
    interpretation = DeterministicConversationInterpreter().interpret(
        "quanto custa esse serviço?"
    )

    result = classify_pending_turn(
        body="quanto custa esse serviço?",
        interpretation=interpretation,
        can_answer_pending=False,
        has_question_answer=True,
        is_clarification=False,
    )

    assert result.relation is TurnRelation.SIDE_QUESTION
    assert result.consumes_repair is False


@pytest.mark.asyncio
async def test_name_reply_pode_ser_joao_does_not_fallback_or_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.CUSTOMER_NAME,
        customer_name=None,
        business_name="Empresa Sintética Alovia",
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(inbound(3001, body="pode ser joao"))

    assert repository.customer_name == "Joao"
    assert repository.state == ConversationState.MENU
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "prazer, joao" in _combined_body(repository)
    assert "desculpa, não entendi" not in _combined_body(repository)
    assert "fallback_variant" not in repository.context


@pytest.mark.asyncio
async def test_service_question_during_name_is_answered_then_name_is_resumed() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.CUSTOMER_NAME,
        customer_name=None,
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Diagnóstico / manutenção corretiva")
    ]
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(3010, body="vocês fazem manutenção em ar condicionado?")
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.CUSTOMER_NAME
    assert repository.customer_name is None
    assert repository.automation_enabled is True
    assert "trabalhamos com diagnóstico / manutenção corretiva" in body
    assert "como posso te chamar" in body
    assert "repair_attempts" not in repository.context

    await engine.process(inbound(3011, body="joao"))

    assert repository.customer_name == "Joao"
    assert repository.state == ConversationState.MENU
    assert repository.automation_enabled is True


@pytest.mark.asyncio
async def test_referral_after_name_is_welcomed_and_does_not_spend_fallback() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        customer_name="Alan",
        business_name="Empresa Sintética Alovia",
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(inbound(3020, body="peguei seu número com meu irmão"))

    body = _combined_body(repository)
    assert repository.state == ConversationState.MENU
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert "fico feliz pela indicação" in body
    assert "como posso te ajudar" in body
    assert "fallback_variant" not in repository.context
    assert "repair_attempts" not in repository.context


@pytest.mark.asyncio
async def test_referral_plus_service_request_keeps_referral_and_advances_service() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        customer_name="Alan",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(
            3030,
            body="meu irmão me indicou vocês e gostaria de uma limpeza de ar condicionado",
        )
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.automation_enabled is True
    assert "fico feliz pela indicação" in body
    assert "marca e o modelo" in body


@pytest.mark.asyncio
async def test_referral_while_date_is_pending_resumes_date_without_repair() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    port = FakeBookingPort()
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(3040, body="meu irmão me indicou vocês"))

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.automation_enabled is True
    assert "fico feliz pela indicação" in body
    assert "repair_attempts" not in repository.context


def _diagnostic_port() -> FakeBookingPort:
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Diagnóstico / manutenção corretiva")
    ]
    port.plan = replace(
        port.plan,
        service=replace(
            port.plan.service,
            estimated_price=Decimal("180.00"),
            pricing_type=PricingType.ESTIMATED,
        ),
    )
    return port


@pytest.mark.asyncio
async def test_clarification_explains_previous_estimated_price_then_resumes_date() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={
            "service_id": str(SERVICE_ID),
            "last_assistant_topic": "estimated_service_price",
        },
        customer_name="Joao",
    )
    engine = ConversationEngine(repository, _diagnostic_port())

    await engine.process(
        inbound(3050, body="como assim se houver condição diferente no local?")
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_DATE
    assert "r$ 180,00" in body
    assert "valor base" in body
    assert "diagnóstico" in body
    assert "repair_attempts" not in repository.context


@pytest.mark.asyncio
async def test_price_ceiling_answers_without_inventing_and_resumes_date() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={
            "service_id": str(SERVICE_ID),
            "last_assistant_topic": "estimated_service_price",
        },
        customer_name="Joao",
    )
    engine = ConversationEngine(repository, _diagnostic_port())

    await engine.process(
        inbound(
            3060,
            body="quero saber da condição diferente. o valor pode chegar até quanto?",
        )
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_DATE
    assert "não tenho um valor máximo cadastrado com segurança" in body
    assert "r$ 180,00" in body
    assert "diagnóstico" in body
    assert "repair_attempts" not in repository.context


@pytest.mark.asyncio
async def test_unknown_parallel_question_does_not_consume_slot_repair() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Joao",
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(inbound(3070, body="tem estacionamento perto?"))

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.automation_enabled is True
    assert "repair_attempts" not in repository.context


@pytest.mark.asyncio
async def test_real_invalid_slot_answer_gets_one_retry_then_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.intake = replace(port.intake, requires_quantity=True)
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(3080, body="banana"))

    assert repository.state == ConversationState.BOOKING_QUANTITY
    assert repository.context["repair_attempts"]["quantity"] == 1
    assert repository.automation_enabled is True

    await engine.process(inbound(3081, body="abacaxi"))

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"

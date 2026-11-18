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
        "pode ser qualquer um",
        "pode ser assim",
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


@pytest.mark.parametrize(
    "body",
    [
        "tudo certo, estou no trabalho",
        "nossa, dormi tão mal",
        "estou trabalhando",
    ],
)
def test_contextual_social_detour_never_consumes_repair(body: str) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    result = classify_pending_turn(
        body=body,
        interpretation=interpretation,
        can_answer_pending=False,
        has_question_answer=False,
        is_clarification=False,
    )

    assert result.relation is TurnRelation.SOCIAL
    assert result.consumes_repair is False


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
async def test_answer_plus_price_question_uses_context_updated_in_same_turn() -> None:
    class QuantityAwarePort(FakeBookingPort):
        async def estimate(self, business_id, service_id, requirements):
            assert service_id == SERVICE_ID
            amount = Decimal("100.00") * requirements.quantity
            return replace(
                self.plan,
                service=replace(
                    self.plan.service,
                    estimated_price=amount,
                    pricing_type=PricingType.ESTIMATED,
                ),
            )

    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Joao",
    )
    port = QuantityAwarePort()
    port.intake = replace(
        port.intake,
        requires_quantity=True,
        requires_address=True,
    )
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(3075, body="são 2 aparelhos, quanto fica?")
    )

    body = _combined_body(repository)
    assert repository.context["quantity"] == 2
    assert repository.context["equipment_quantity"] == 2
    assert repository.state == ConversationState.BOOKING_WEEKDAY
    assert "service_address" not in repository.context
    assert "r$ 200,00" in body
    assert "r$ 100,00" not in body


@pytest.mark.asyncio
async def test_real_quote_journey_keeps_sales_intent_until_installation_qualification() -> None:
    repository = FakeConversationRepository(
        customer_name=None,
        business_name="Empresa Sintética Alovia",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    port.intake = replace(
        port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(
            3079,
            body="Boa tarde! Tudo bem? Precisava retomar a cotação do ar condicionado",
        )
    )

    assert repository.state == ConversationState.CUSTOMER_NAME
    assert "cotação" in repository.context["pending_customer_message"].casefold()

    await engine.process(inbound(3080, body="Joao"))

    assert repository.customer_name == "Joao"
    assert repository.state == ConversationState.BOOKING_SERVICE
    assert repository.context["request_mode"] == "quote"
    assert repository.context["service_clarification"] == "equipment_purchase"
    scope_body = _combined_body(repository)
    assert "entendi que você quer comprar" not in scope_body
    assert "r$ 700,00" not in scope_body
    assert "para eu entender sua cotação" in scope_body
    assert "só a instalação" in scope_body
    assert "instalação" in scope_body

    await engine.process(
        inbound(
            3081,
            action="equipment.installation",
            body="Só instalação",
        )
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["request_mode"] == "quote"
    assert repository.context["equipment_ownership"] == "has_equipment"
    assert "service_address" not in repository.context
    assert "capacidade em btus" in body
    assert "montar a cotação" in body
    assert "endereço" not in body


@pytest.mark.asyncio
async def test_installation_quote_preserves_quote_and_qualifies_before_address() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={
            "service_clarification": "equipment_purchase",
            "request_mode": "quote",
        },
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    port.intake = replace(
        port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(
            3076,
            action="equipment.installation",
            body="Só instalação",
        )
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["request_mode"] == "quote"
    assert repository.context["equipment_ownership"] == "has_equipment"
    assert "service_address" not in repository.context
    assert "capacidade em btus" in body
    assert "montar a cotação" in body
    assert "endereço" not in body


@pytest.mark.asyncio
async def test_cleaning_lead_is_qualified_before_service_address() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    port.intake = replace(
        port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(3077, body="quero fazer uma limpeza no meu ar condicionado")
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert "service_address" not in repository.context
    assert "marca e o modelo" in body
    assert "endereço" not in body


@pytest.mark.asyncio
async def test_quote_is_presented_before_service_address() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_PROPERTY,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "has_equipment",
            "equipment_model": "LG 12000 BTUs",
            "installation_height_over_3m": False,
            "tube_disclaimer_sent": True,
            "tubing_length_answered": True,
        },
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    port.intake = replace(
        port.intake,
        requires_address=True,
        asks_tubing_length=False,
    )
    engine = ConversationEngine(repository, port)

    await engine.process(
        inbound(3078, action="property.house", body="Casa")
    )

    body = _combined_body(repository)
    assert repository.state == ConversationState.QUOTE_DECISION
    assert repository.context["property_type"] == "house"
    assert repository.context["quote_presented"] is True
    assert "service_address" not in repository.context
    assert "valor do serviço" in body.casefold()
    assert "agendar a instalação" in body.casefold()
    assert "endereço completo" not in body.casefold()


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

@pytest.mark.asyncio
async def test_service_details_side_question_answers_and_resumes_without_repair() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE_DETAILS,
        context={
            "service_id": str(SERVICE_ID),
            "service_qualification_version": 2,
        },
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Atendimento técnico especial")
    ]
    port.intake = replace(port.intake, requires_address=True)
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(3090, body="quanto custa esse serviço?"))

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_SERVICE_DETAILS
    assert "repair_attempts" not in repository.context
    assert "100,00" in body
    assert "antes de pedir o endereço" in body or "o que precisa ser feito" in body


@pytest.mark.asyncio
async def test_service_details_social_detour_resumes_without_repair() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE_DETAILS,
        context={
            "service_id": str(SERVICE_ID),
            "service_qualification_version": 2,
        },
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Atendimento técnico especial")
    ]
    port.intake = replace(port.intake, requires_address=True)
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(3091, body="valeu, só um minuto"))

    body = _combined_body(repository)
    assert repository.state == ConversationState.BOOKING_SERVICE_DETAILS
    assert "repair_attempts" not in repository.context
    assert "continu" in body or "o que precisa ser feito" in body


@pytest.mark.asyncio
async def test_service_details_real_failure_retries_once_then_handoffs() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE_DETAILS,
        context={
            "service_id": str(SERVICE_ID),
            "service_qualification_version": 2,
        },
        customer_name="Joao",
    )
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Atendimento técnico especial")
    ]
    port.intake = replace(port.intake, requires_address=True)
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(3092, body="banana"))

    assert repository.state == ConversationState.BOOKING_SERVICE_DETAILS
    assert repository.context["repair_attempts"]["service_details"] == 1
    assert repository.automation_enabled is True

    await engine.process(inbound(3093, body="abacaxi"))

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"

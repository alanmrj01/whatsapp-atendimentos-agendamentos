"""Discovery corpus for commercial side questions during booking checkpoints.

The assertions in this module describe customer-visible robustness properties.  A
failure is intentionally left visible when the deterministic engine cannot yet
answer a known fact or safely distinguish an unsupported commercial question.
"""

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
)
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
        {
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
        "09:00",
        id="time",
    ),
)


@pytest.mark.parametrize(
    ("body", "intent", "budget_field", "budget"),
    [
        pytest.param(
            "Quanto custa esse serviço?",
            ConversationIntent.PRICE_QUESTION,
            None,
            None,
            id="direct-price",
        ),
        pytest.param(
            "Pode fazer um orçamento da manutenção?",
            ConversationIntent.PRICE_QUESTION,
            None,
            None,
            id="quote-request",
        ),
        pytest.param(
            "Meu limite para o serviço é R$ 150",
            ConversationIntent.PRICE_QUESTION,
            "service_budget_max",
            150.0,
            id="service-budget",
        ),
        pytest.param(
            "Meu orçamento total é de R$ 4.000 com instalação",
            ConversationIntent.PRICE_QUESTION,
            "total_budget_max",
            4000.0,
            id="total-budget",
        ),
        pytest.param(
            "Só posso gastar R$ 80 no serviço, quanto fica?",
            ConversationIntent.PRICE_QUESTION,
            "service_budget_max",
            80.0,
            id="conversational-service-budget-limit",
        ),
        pytest.param(
            "Quanto tempo leva o serviço?",
            ConversationIntent.DURATION_QUESTION,
            None,
            None,
            id="duration",
        ),
        pytest.param(
            "O que está incluso nesse serviço?",
            ConversationIntent.SERVICE_QUESTION,
            None,
            None,
            id="inclusions",
        ),
        pytest.param(
            "A tubulação está inclusa?",
            ConversationIntent.TECHNICAL_QUESTION,
            None,
            None,
            id="included-tubing",
        ),
        pytest.param(
            "Quanto custa cada metro adicional?",
            ConversationIntent.PRICE_QUESTION,
            None,
            None,
            id="additional-material-price",
        ),
        pytest.param(
            "A visita técnica é cobrada?",
            ConversationIntent.PRICE_QUESTION,
            None,
            None,
            id="visit-fee",
        ),
        pytest.param(
            "Tem garantia?",
            ConversationIntent.TECHNICAL_QUESTION,
            None,
            None,
            id="warranty",
        ),
    ],
)
def test_commercial_side_question_language_is_classified_without_guessing(
    body: str,
    intent: ConversationIntent,
    budget_field: str | None,
    budget: float | None,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(intent)
    assert interpretation.has_act(ConversationAct.SIDE_QUESTION)
    if budget_field is not None:
        assert getattr(interpretation, budget_field) == budget


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("Dou muito valor a um atendimento bem feito", id="value-opinion"),
        pytest.param("O endereço é Rua Quinze de Novembro, 150", id="address-number"),
        pytest.param("O aparelho tem 12.000 BTUs", id="equipment-capacity"),
        pytest.param("Tenho uma visita da família amanhã", id="family-visit"),
    ],
)
def test_noncommercial_numbers_and_words_do_not_become_price_or_duration(body: str) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert not interpretation.has(ConversationIntent.PRICE_QUESTION)
    assert not interpretation.has(ConversationIntent.DURATION_QUESTION)
    assert interpretation.service_budget_max is None
    assert interpretation.total_budget_max is None
    assert interpretation.equipment_budget_max is None


@pytest.mark.parametrize(("state", "context", "resume_fragment"), CHECKPOINTS)
@pytest.mark.parametrize(
    ("body", "answer_fragment"),
    [
        pytest.param("Quanto custa esse serviço?", "r$ 100,00", id="price"),
        pytest.param("Quanto tempo demora?", "30 minutos", id="duration"),
        pytest.param(
            "O que está incluso nesse serviço?",
            "serviço configurado para teste",
            id="catalog-description",
        ),
        pytest.param(
            "A tubulação está inclusa?",
            "3 metros",
            id="known-included-tubing",
        ),
        pytest.param(
            "Quanto custa cada metro adicional?",
            "r$ 20,00",
            id="known-additional-material",
        ),
        pytest.param(
            "Tem garantia?",
            "não está estruturado",
            id="unsupported-warranty-safe-answer",
        ),
    ],
)
@pytest.mark.asyncio
async def test_side_question_answers_known_fact_and_resumes_exact_checkpoint(
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

    await ConversationEngine(repository, booking_port).process(inbound(1, body=body))

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state == state
    assert repository.context == before
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"
    assert answer_fragment in response
    assert resume_fragment.casefold() in response


@pytest.mark.parametrize(("state", "context", "resume_fragment"), CHECKPOINTS)
@pytest.mark.asyncio
async def test_visit_fee_without_registered_source_is_not_invented_or_consumed(
    state: ConversationState,
    context: dict[str, object],
    resume_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=state is ConversationState.BOOKING_ADDRESS,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(2, body="A visita técnica é cobrada?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state == state
    assert repository.context == context
    assert "r$ 100,00" not in response
    assert any(
        safe in response
        for safe in (
            "não está estruturado",
            "não está cadastrado",
            "não consigo confirmar",
            "equipe pode confirmar",
        )
    )
    assert resume_fragment.casefold() in response


@pytest.mark.parametrize(("state", "context", "resume_fragment"), CHECKPOINTS)
@pytest.mark.asyncio
async def test_price_without_registered_value_uses_human_quote_and_resumes(
    state: ConversationState,
    context: dict[str, object],
    resume_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=state is ConversationState.BOOKING_ADDRESS,
        pricing_type=PricingType.HUMAN_QUOTE,
    )
    booking_port.plan = replace(
        booking_port.plan,
        service=replace(
            booking_port.plan.service,
            estimated_price=None,
            pricing_type=PricingType.HUMAN_QUOTE,
            requires_human_quote=True,
        ),
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(3, body="Qual é o preço desse serviço?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state == state
    assert repository.context == context
    assert "depende de uma avaliação" in response
    assert "r$" not in response
    assert resume_fragment.casefold() in response


@pytest.mark.asyncio
async def test_price_and_duration_in_one_turn_are_both_answered_before_resuming() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(4, body="Quanto custa e quanto tempo demora?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.state == ConversationState.BOOKING_TIME
    assert "r$ 100,00" in response
    assert "30 minutos" in response
    assert "09:00" in response

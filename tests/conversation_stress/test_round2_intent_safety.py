from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    SERVICE_ID,
    FakeBookingPort,
    FakeConversationRepository,
    confirmation_context,
    inbound,
)


DESTRUCTIVE_CALLS = {"cancel_booking", "reschedule_booking"}


def _destructive_calls(port: FakeBookingPort) -> list[str]:
    return [call for call in port.calls if call in DESTRUCTIVE_CALLS]


def _installation_port() -> FakeBookingPort:
    port = FakeBookingPort()
    port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    port.intake = replace(port.intake, requires_quantity=False)
    return port


@pytest.mark.parametrize(
    ("body", "question_intent", "action_intent"),
    [
        (
            "Posso cancelar depois?",
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.CANCEL,
        ),
        (
            "Como funciona o cancelamento?",
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.CANCEL,
        ),
        (
            "Tem como cancelar sem multa?",
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.CANCEL,
        ),
        (
            "Se eu cancelar, pago alguma taxa?",
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.CANCEL,
        ),
        (
            "Vocês conseguem cancelar?",
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.CANCEL,
        ),
        (
            "Posso remarcar depois?",
            ConversationIntent.RESCHEDULE_QUESTION,
            ConversationIntent.RESCHEDULE,
        ),
        (
            "Como funciona o reagendamento?",
            ConversationIntent.RESCHEDULE_QUESTION,
            ConversationIntent.RESCHEDULE,
        ),
        (
            "Qual é o prazo para remarcar?",
            ConversationIntent.RESCHEDULE_QUESTION,
            ConversationIntent.RESCHEDULE,
        ),
        (
            "Queria saber se dá para reagendar",
            ConversationIntent.RESCHEDULE_QUESTION,
            ConversationIntent.RESCHEDULE,
        ),
    ],
)
def test_destructive_action_questions_are_side_questions(
    body: str,
    question_intent: ConversationIntent,
    action_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(question_intent)
    assert not interpretation.has(action_intent)
    assert interpretation.has_act(ConversationAct.SIDE_QUESTION)


@pytest.mark.parametrize(
    ("body", "forbidden_intent"),
    [
        ("não quero cancelar", ConversationIntent.CANCEL),
        ("não vou desmarcar", ConversationIntent.CANCEL),
        ("não precisa cancelar", ConversationIntent.CANCEL),
        ("não é para cancelar", ConversationIntent.CANCEL),
        ("desisti de cancelar", ConversationIntent.CANCEL),
        ("não cancela não, vou manter", ConversationIntent.CANCEL),
        ("não quero remarcar", ConversationIntent.RESCHEDULE),
        ("não vou reagendar", ConversationIntent.RESCHEDULE),
        ("não precisa remarcar", ConversationIntent.RESCHEDULE),
    ],
)
def test_negated_destructive_language_is_never_an_action(
    body: str,
    forbidden_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert not interpretation.has(forbidden_intent)
    assert interpretation.has_act(ConversationAct.NEGATED_ACTION)


@pytest.mark.parametrize(
    ("body", "expected_intent"),
    [
        ("Quero cancelar minha visita", ConversationIntent.CANCEL),
        ("Preciso desmarcar o atendimento", ConversationIntent.CANCEL),
        ("Pode cancelar minha visita?", ConversationIntent.CANCEL),
        ("Cancela pra mim, por favor", ConversationIntent.CANCEL),
        ("Quero remarcar minha visita", ConversationIntent.RESCHEDULE),
        ("Preciso reagendar o atendimento", ConversationIntent.RESCHEDULE),
        ("Pode remarcar para mim?", ConversationIntent.RESCHEDULE),
        ("Quero trocar o horário", ConversationIntent.RESCHEDULE),
    ],
)
def test_explicit_destructive_commands_remain_actions(
    body: str,
    expected_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(expected_intent)


@pytest.mark.parametrize(
    "body",
    [
        "não tenho aparelho",
        "nao tenho o ar",
    ],
)
@pytest.mark.asyncio
async def test_equipment_ownership_negation_means_customer_needs_equipment(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
        },
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(100, body=body)
    )

    assert repository.context["equipment_ownership"] == "needs_equipment"
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.parametrize(
    "body",
    [
        "Tenho uma dúvida: quanto custa?",
        "Não tenho certeza se cabe no quarto",
        "Hoje tenho só quinze minutos para conversar",
        "Vocês têm garantia?",
    ],
)
@pytest.mark.asyncio
async def test_incidental_tenho_does_not_answer_equipment_ownership(
    body: str,
) -> None:
    context = {
        "service_id": str(SERVICE_ID),
        "request_mode": "quote",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        context=context,
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(110, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_OWNERSHIP
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert "equipment_ownership" not in repository.context
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    "body",
    [
        "Tem como cancelar sem multa?",
        "Se eu cancelar, pago alguma taxa?",
        "Qual é o prazo para remarcar?",
        "Queria saber se dá para reagendar",
    ],
)
@pytest.mark.asyncio
async def test_action_question_preserves_booking_confirmation(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
        customer_name="Alan",
    )
    original_context = deepcopy(repository.context)
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(200, body=body)
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context == original_context
    assert _destructive_calls(port) == []


@pytest.mark.parametrize(
    "body",
    [
        "não precisa cancelar",
        "não é para cancelar",
        "desisti de cancelar",
        "não precisa remarcar",
    ],
)
@pytest.mark.asyncio
async def test_negated_action_preserves_schedule_checkpoint_and_never_mutates(
    body: str,
) -> None:
    context = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context=context,
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(210, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.automation_enabled is True
    assert _destructive_calls(port) == []


@pytest.mark.parametrize(
    ("state", "body"),
    [
        (ConversationState.CANCEL, "não cancela não, vou manter"),
        (ConversationState.CANCEL, "se eu cancelar, pago alguma taxa?"),
        (ConversationState.RESCHEDULE, "não quero remarcar"),
        (ConversationState.RESCHEDULE, "posso remarcar depois?"),
    ],
)
@pytest.mark.asyncio
async def test_confirmation_state_never_executes_on_negation_or_question(
    state: ConversationState,
    body: str,
) -> None:
    context = {
        "appointment_id": "50000000-0000-0000-0000-000000000005",
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    repository = FakeConversationRepository(state=state, context=context)
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(220, body=body)
    )

    assert _destructive_calls(port) == []

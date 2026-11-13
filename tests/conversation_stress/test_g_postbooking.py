"""Discovery corpus for confirmation and post-booking conversation safety.

This module intentionally records deterministic product gaps as failing tests.
It must not contact production services or mutate runtime code.
"""

from __future__ import annotations

from copy import deepcopy

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
    APPOINTMENT_ID,
    SERVICE_ID,
    FakeBookingPort,
    FakeConversationRepository,
    confirmation_context,
    inbound,
)


DESTRUCTIVE_CALLS = {"confirm", "cancel_booking", "reschedule_booking"}


def _destructive_calls(port: FakeBookingPort) -> list[str]:
    return [call for call in port.calls if call in DESTRUCTIVE_CALLS]


def _booking_confirmation_repository() -> FakeConversationRepository:
    return FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
        customer_name="Alan",
    )


@pytest.mark.parametrize(
    ("body", "question_intent", "action_intent"),
    [
        ("Posso cancelar depois?", ConversationIntent.CANCEL_QUESTION, ConversationIntent.CANCEL),
        ("Como funciona o cancelamento?", ConversationIntent.CANCEL_QUESTION, ConversationIntent.CANCEL),
        ("Tem como cancelar sem multa?", ConversationIntent.CANCEL_QUESTION, ConversationIntent.CANCEL),
        ("Se eu cancelar, pago alguma taxa?", ConversationIntent.CANCEL_QUESTION, ConversationIntent.CANCEL),
        ("Vocês conseguem cancelar?", ConversationIntent.CANCEL_QUESTION, ConversationIntent.CANCEL),
        ("Posso remarcar depois?", ConversationIntent.RESCHEDULE_QUESTION, ConversationIntent.RESCHEDULE),
        ("Como funciona o reagendamento?", ConversationIntent.RESCHEDULE_QUESTION, ConversationIntent.RESCHEDULE),
        ("Qual é o prazo para remarcar?", ConversationIntent.RESCHEDULE_QUESTION, ConversationIntent.RESCHEDULE),
        ("Queria saber se dá para reagendar", ConversationIntent.RESCHEDULE_QUESTION, ConversationIntent.RESCHEDULE),
    ],
    ids=[
        "cancel-posso",
        "cancel-como-funciona",
        "cancel-tem-como",
        "cancel-condicional-taxa",
        "cancel-conseguem",
        "reschedule-posso",
        "reschedule-como-funciona",
        "reschedule-prazo",
        "reschedule-queria-saber",
    ],
)
def test_questions_about_destructive_actions_are_not_action_requests(
    body: str,
    question_intent: ConversationIntent,
    action_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(question_intent)
    assert not interpretation.has(action_intent)
    assert interpretation.has_act(ConversationAct.SIDE_QUESTION)


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
    ids=[
        "cancel-quero",
        "cancel-preciso",
        "cancel-pode",
        "cancel-imperativo",
        "reschedule-quero",
        "reschedule-preciso",
        "reschedule-pode",
        "reschedule-trocar-horario",
    ],
)
def test_explicit_destructive_requests_are_recognized_without_executing_them(
    body: str,
    expected_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(expected_intent)


@pytest.mark.parametrize(
    ("body", "forbidden_intent"),
    [
        ("Não quero cancelar", ConversationIntent.CANCEL),
        ("Não vou desmarcar", ConversationIntent.CANCEL),
        ("Não precisa cancelar", ConversationIntent.CANCEL),
        ("Não é para cancelar", ConversationIntent.CANCEL),
        ("Desisti de cancelar", ConversationIntent.CANCEL),
        ("Não quero remarcar", ConversationIntent.RESCHEDULE),
        ("Não vou reagendar", ConversationIntent.RESCHEDULE),
        ("Não precisa remarcar", ConversationIntent.RESCHEDULE),
        ("Não cancela não, vou manter", ConversationIntent.CANCEL),
    ],
    ids=[
        "cancel-nao-quero",
        "cancel-nao-vou",
        "cancel-nao-precisa",
        "cancel-nao-e-para",
        "cancel-desisti",
        "reschedule-nao-quero",
        "reschedule-nao-vou",
        "reschedule-nao-precisa",
        "cancel-nao-cancela-nao",
    ],
)
def test_negated_destructive_language_never_becomes_an_action(
    body: str,
    forbidden_intent: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert not interpretation.has(forbidden_intent)


@pytest.mark.parametrize(
    "body",
    [
        "Confirmar",
        "Sim",
        "Pode confirmar por favor",
        "Está tudo certo, pode finalizar",
        "Fechado",
    ],
    ids=["confirmar", "sim", "pode-confirmar", "tudo-certo", "fechado"],
)
@pytest.mark.asyncio
async def test_final_booking_confirmation_requires_an_explicit_affirmative(body: str) -> None:
    repository = _booking_confirmation_repository()
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(100, body=body))

    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert port.calls.count("confirm") == 1


@pytest.mark.parametrize(
    ("message", "expected_state"),
    [
        ({"action": "booking.cancel"}, ConversationState.MENU),
        ({"action": "booking.back"}, ConversationState.BOOKING_TIME),
        ({"body": "não"}, ConversationState.MENU),
    ],
    ids=["button-cancel", "button-back", "text-no"],
)
@pytest.mark.asyncio
async def test_booking_confirmation_exit_paths_never_create_an_appointment(
    message: dict[str, str],
    expected_state: ConversationState,
) -> None:
    repository = _booking_confirmation_repository()
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(110, **message))

    assert repository.state == expected_state
    assert "confirm" not in port.calls


@pytest.mark.parametrize(
    "body",
    [
        "Posso cancelar depois?",
        "Como funciona o cancelamento?",
        "Tem como cancelar sem multa?",
        "Se eu cancelar, pago alguma taxa?",
    ],
    ids=["posso", "como-funciona", "tem-como", "condicional-taxa"],
)
@pytest.mark.asyncio
async def test_cancel_questions_during_confirmation_preserve_the_booking_checkpoint(
    body: str,
) -> None:
    repository = _booking_confirmation_repository()
    original_context = deepcopy(repository.context)
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(120, body=body))

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context == original_context
    assert _destructive_calls(port) == []


@pytest.mark.asyncio
async def test_post_booking_decline_completes_without_new_destructive_action() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context=confirmation_context(),
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(130, body="Não, obrigado. Era só isso")
    )

    assert repository.state == ConversationState.COMPLETED
    assert _destructive_calls(port) == []


@pytest.mark.asyncio
async def test_post_booking_new_service_starts_clean_without_old_booking_context() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context={
            **confirmation_context(),
            "appointment_id": str(APPOINTMENT_ID),
            "old_transaction_marker": True,
        },
    )
    port = FakeBookingPort()
    port.services = [BookingOption(str(SERVICE_ID), "Limpeza e higienização")]

    await ConversationEngine(repository, port).process(
        inbound(131, body="Também gostaria de fazer uma limpeza")
    )

    assert repository.state != ConversationState.HUMAN_HANDOFF
    assert "appointment_id" not in repository.context
    assert "old_transaction_marker" not in repository.context
    assert _destructive_calls(port) == []


@pytest.mark.parametrize(
    "body",
    [
        "Posso cancelar esse agendamento depois?",
        "Tem como remarcar sem perder a visita?",
    ],
    ids=["cancel-question", "reschedule-question"],
)
@pytest.mark.asyncio
async def test_post_booking_questions_answer_without_starting_existing_booking_flow(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context=confirmation_context(),
    )
    original_context = deepcopy(repository.context)
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(132, body=body))

    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert repository.context == original_context
    assert "existing_bookings" not in port.calls
    assert _destructive_calls(port) == []


@pytest.mark.parametrize(
    "body",
    [
        "Bom dia",
        "Quero agendar uma limpeza",
    ],
    ids=["greeting", "new-service"],
)
@pytest.mark.asyncio
async def test_completed_conversation_starts_a_fresh_journey(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        context={
            "appointment_id": str(APPOINTMENT_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": confirmation_context()["candidate_booking"],
        },
    )
    port = FakeBookingPort()
    port.services = [BookingOption(str(SERVICE_ID), "Limpeza e higienização")]

    await ConversationEngine(repository, port).process(inbound(140, body=body))

    assert repository.state not in {
        ConversationState.COMPLETED,
        ConversationState.HUMAN_HANDOFF,
    }
    assert "appointment_id" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert _destructive_calls(port) == []


@pytest.mark.parametrize(
    "body",
    [
        "não",
        "não cancela não, vou manter",
        "se eu cancelar, pago alguma taxa?",
    ],
    ids=["plain-no", "explicit-keep", "conditional-question"],
)
@pytest.mark.asyncio
async def test_cancel_confirmation_never_cancels_on_negation_or_question(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.CANCEL,
        context={"appointment_id": str(APPOINTMENT_ID)},
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(150, body=body))

    assert "cancel_booking" not in port.calls


@pytest.mark.parametrize(
    ("body", "must_reschedule"),
    [
        ("confirmar", True),
        ("não", False),
        ("não quero remarcar", False),
    ],
    ids=["confirm", "plain-no", "negated-reschedule"],
)
@pytest.mark.asyncio
async def test_reschedule_confirmation_only_executes_on_explicit_confirmation(
    body: str,
    must_reschedule: bool,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.RESCHEDULE,
        context={
            "appointment_id": str(APPOINTMENT_ID),
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
        },
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(inbound(160, body=body))

    assert ("reschedule_booking" in port.calls) is must_reschedule


@pytest.mark.asyncio
async def test_natural_time_correction_does_not_confirm_the_old_booking() -> None:
    repository = _booking_confirmation_repository()
    port = FakeBookingPort()
    port.times = [
        BookingOption("09:00", "09:00"),
        BookingOption("10:00", "10:00"),
    ]

    await ConversationEngine(repository, port).process(
        inbound(170, body="Não é 09:00, é 10:00")
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context["selected_time"] == "10:00"
    assert repository.context["candidate_booking"]["selected_time"] == "10:00"
    assert "confirm" not in port.calls

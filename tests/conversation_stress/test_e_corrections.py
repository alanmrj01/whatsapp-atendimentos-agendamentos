"""Rodada 1 — correções, contradições e múltiplas intenções.

As expectativas que falham abaixo são gaps de descoberta confirmados no
baseline. Elas permanecem vermelhas de propósito, sem transformar um
comportamento ausente em sucesso silencioso. Nenhum ajuste de runtime pertence
a esta rodada.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.conversations.constants import ConversationState
from app.conversations.context_policy import (
    context_for_service_change,
    invalidate_changed_facts,
)
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


MAINTENANCE_ID = uuid.UUID("41000000-0000-0000-0000-000000000004")
GAS_RECHARGE_ID = uuid.UUID("42000000-0000-0000-0000-000000000004")
INSTALLATION_ID = uuid.UUID("43000000-0000-0000-0000-000000000004")

OLD_ADDRESS = {
    "address_line": "Rua Antiga",
    "number": "10",
    "neighborhood": "Centro",
    "city": "São José dos Campos",
    "state": "SP",
}


class MultiServiceBookingPort(FakeBookingPort):
    """Fake existente com asserts relaxados apenas para troca de serviço."""

    def __init__(self) -> None:
        super().__init__()
        self.services = [
            BookingOption(str(SERVICE_ID), "Limpeza e higienização"),
            BookingOption(
                str(MAINTENANCE_ID),
                "Diagnóstico e manutenção corretiva",
            ),
            BookingOption(str(GAS_RECHARGE_ID), "Recarga de gás"),
            BookingOption(
                str(INSTALLATION_ID),
                "Instalação de ar-condicionado split",
            ),
        ]

    async def get_service_intake(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
    ):
        return self.intake

    async def estimate(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: Any,
    ):
        return self.plan

    async def list_dates(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: Any,
    ) -> tuple[BookingOption, ...]:
        self.calls.append("dates")
        return tuple(self.dates)


@pytest.mark.parametrize(
    ("body", "service_key"),
    [
        ("não é limpeza, é manutenção", "diagnostics"),
        ("Não quero compra, quero instalação", "split-installation"),
        ("Na verdade preciso de recarga de gás", "gas-recharge"),
        ("quis dizer limpeza", "cleaning"),
        ("corrigindo: manutenção", "diagnostics"),
        ("melhor dizendo, instalação", "split-installation"),
        ("não é recarga de gás; quero limpeza", "cleaning"),
        ("não é manutenção, prefiro limpeza", "cleaning"),
        ("Eu disse limpeza; não manutenção", "cleaning"),
        ("o serviço é manutenção, não limpeza", "diagnostics"),
    ],
)
def test_latest_explicit_service_assertion_wins(
    body: str,
    service_key: str,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.service_key == service_key
    assert interpretation.has(ConversationIntent.SERVICE_INTENT)
    assert interpretation.has_act(ConversationAct.CORRECTION)


@pytest.mark.parametrize(
    ("body", "service_key"),
    [
        ("também quero manutenção", "diagnostics"),
        ("além disso quero limpeza", "cleaning"),
        ("também preciso instalar um split", "split-installation"),
        ("além disso gostaria de recarga de gás", "gas-recharge"),
        ("bom dia, também quero uma limpeza", "cleaning"),
    ],
)
def test_prefixed_additional_request_is_classified(
    body: str,
    service_key: str,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.service_key == service_key
    assert interpretation.has_act(ConversationAct.ADDITIONAL_REQUEST)


def test_trailing_additional_marker_is_still_an_additional_request() -> None:
    interpretation = DeterministicConversationInterpreter().interpret(
        "quero manutenção também"
    )

    assert interpretation.service_key == "diagnostics"
    assert interpretation.has_act(ConversationAct.ADDITIONAL_REQUEST)


@pytest.mark.parametrize(
    ("body", "forbidden"),
    [
        ("não vou desmarcar", ConversationIntent.CANCEL),
        ("não pretendo reagendar", ConversationIntent.RESCHEDULE),
        ("não desejo falar com atendente", ConversationIntent.HUMAN_HANDOFF),
        ("não quero cancelar, mantenha como está", ConversationIntent.CANCEL),
        ("não quero remarcar, mantenha como está", ConversationIntent.RESCHEDULE),
    ],
)
def test_negated_operation_never_becomes_destructive_intent(
    body: str,
    forbidden: ConversationIntent,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert not interpretation.has(forbidden)
    assert interpretation.has_act(ConversationAct.NEGATED_ACTION)


def test_indirect_cancel_negation_never_becomes_cancel_intent() -> None:
    interpretation = DeterministicConversationInterpreter().interpret(
        "não é para cancelar"
    )

    assert not interpretation.has(ConversationIntent.CANCEL)
    assert interpretation.has_act(ConversationAct.NEGATED_ACTION)


@pytest.mark.parametrize(
    ("body", "pending_id"),
    [
        ("Também quero manutenção", MAINTENANCE_ID),
        ("Além disso quero recarga de gás", GAS_RECHARGE_ID),
        ("também preciso instalar um split", INSTALLATION_ID),
        ("bom dia, além disso gostaria de manutenção", MAINTENANCE_ID),
    ],
)
@pytest.mark.asyncio
async def test_additional_service_preserves_active_request_until_confirmation(
    body: str,
    pending_id: uuid.UUID,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "service_address": OLD_ADDRESS,
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(100, body=body)
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["quantity"] == 2
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["pending_service_change_id"] == str(pending_id)


@pytest.mark.asyncio
async def test_contradictory_service_correction_selects_latest_assertion() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "service_address": OLD_ADDRESS,
            "property_type": "house",
            "selected_date": "2026-09-02",
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(199, body="não é limpeza, é manutenção")
    )

    assert repository.context["service_id"] == str(MAINTENANCE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS


@pytest.mark.parametrize(
    ("body", "expected_id"),
    [
        ("na verdade preciso de recarga de gás", GAS_RECHARGE_ID),
        ("quis dizer instalação de split", INSTALLATION_ID),
        ("corrigindo: manutenção", MAINTENANCE_ID),
    ],
)
@pytest.mark.asyncio
async def test_service_correction_keeps_only_safe_independent_facts(
    body: str,
    expected_id: uuid.UUID,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "service_address": OLD_ADDRESS,
            "property_type": "house",
            "onsite_contact_name": "Marina",
            "quantity": 2,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(200, body=body)
    )

    assert repository.context["service_id"] == str(expected_id)
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["property_type"] == "house"
    assert repository.context["onsite_contact_name"] == "Marina"
    assert "quantity" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("na verdade são 3 aparelhos", 3),
        ("corrigindo: preciso limpar quatro unidades", 4),
        ("não são 2, são cinco aparelhos", 5),
        ("quis dizer 6 equipamentos", 6),
    ],
)
@pytest.mark.asyncio
async def test_quantity_correction_updates_canonical_fact_and_invalidates_schedule(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "equipment_quantity": 2,
            "service_address": OLD_ADDRESS,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(300, body=body)
    )

    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context


@pytest.mark.parametrize(
    "body",
    [
        "corrigindo: o endereço é Rua Nova, 25, Centro, Jacareí - SP",
        "na verdade é Avenida Brasil, 300, Jardim Sul, São José dos Campos - SP",
        "o endereço não é o antigo; é Rua das Flores, 48, Centro, Taubaté - SP",
    ],
)
@pytest.mark.asyncio
async def test_address_correction_preserves_service_and_invalidates_route_dependent_facts(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "property_type": "house",
            "service_address": OLD_ADDRESS,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(400, body=body)
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["quantity"] == 2
    assert repository.context["property_type"] == "house"
    assert repository.context["service_address"] != OLD_ADDRESS
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context


@pytest.mark.parametrize(
    "body",
    [
        "não vou desmarcar",
        "não pretendo reagendar",
        "não desejo falar com atendente",
        "não quero cancelar, pode seguir",
    ],
)
@pytest.mark.asyncio
async def test_negated_operation_preserves_checkpoint_and_never_calls_mutation(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "service_address": OLD_ADDRESS,
        },
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(500, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["service_address"] == OLD_ADDRESS
    assert "cancel_booking" not in booking_port.calls
    assert "reschedule_booking" not in booking_port.calls


@pytest.mark.asyncio
async def test_service_correction_plus_price_question_applies_correction_then_resumes() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID), "service_address": OLD_ADDRESS},
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(600, body="na verdade é manutenção; quanto custa esse serviço?")
    )

    assert repository.context["service_id"] == str(MAINTENANCE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS


@pytest.mark.asyncio
async def test_additional_service_plus_price_question_keeps_current_and_records_pending() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID), "service_address": OLD_ADDRESS},
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(601, body="também quero manutenção; quanto custa?")
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["pending_service_change_id"] == str(MAINTENANCE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS


@pytest.mark.asyncio
async def test_quantity_correction_plus_price_question_updates_quantity_before_resuming() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "equipment_quantity": 2,
            "service_address": OLD_ADDRESS,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(602, body="corrigindo: são 3 aparelhos; e quanto custa?")
    )

    assert repository.context["quantity"] == 3
    assert repository.context["equipment_quantity"] == 3
    assert repository.context["service_address"] == OLD_ADDRESS


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("quantity", 3),
        ("access_condition", "difficult"),
        (
            "service_address",
            {**OLD_ADDRESS, "address_line": "Rua Nova", "number": "25"},
        ),
    ],
)
def test_context_policy_invalidates_schedule_but_preserves_independent_facts(
    changed: str,
    value: object,
) -> None:
    previous = {
        "service_id": str(SERVICE_ID),
        "quantity": 2,
        "access_condition": "normal",
        "service_address": OLD_ADDRESS,
        "property_type": "house",
        "onsite_contact_name": "Marina",
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
        "candidate_booking": {"service_id": str(SERVICE_ID)},
        "quote_presented": True,
    }

    updated = invalidate_changed_facts(previous, {**previous, changed: value})

    assert updated[changed] == value
    assert updated["service_id"] == str(SERVICE_ID)
    assert updated["property_type"] == "house"
    assert updated["onsite_contact_name"] == "Marina"
    assert "selected_date" not in updated
    assert "selected_time" not in updated
    assert "candidate_booking" not in updated
    assert "quote_presented" not in updated


def test_service_change_policy_keeps_only_documented_reusable_facts() -> None:
    context = {
        "service_id": str(SERVICE_ID),
        "service_address": OLD_ADDRESS,
        "property_type": "house",
        "onsite_contact_name": "Marina",
        "whatsapp_contact_phone": "synthetic-test-id",
        "quantity": 2,
        "access_condition": "difficult",
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
        "candidate_booking": {"service_id": str(SERVICE_ID)},
        "quote_presented": True,
    }

    updated = context_for_service_change(context)

    assert updated == {
        "service_address": OLD_ADDRESS,
        "property_type": "house",
        "onsite_contact_name": "Marina",
        "whatsapp_contact_phone": "synthetic-test-id",
    }

"""Rodada 2C — perguntas laterais, múltiplos atos e números tipados."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

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
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


MAINTENANCE_ID = uuid.UUID("41000000-0000-0000-0000-000000000004")
GAS_RECHARGE_ID = uuid.UUID("42000000-0000-0000-0000-000000000004")


class MultiServiceBookingPort(FakeBookingPort):
    def __init__(self, *, requires_quantity: bool = False) -> None:
        super().__init__()
        self.services = [
            BookingOption(str(SERVICE_ID), "Limpeza e higienização"),
            BookingOption(
                str(MAINTENANCE_ID),
                "Diagnóstico e manutenção corretiva",
            ),
            BookingOption(str(GAS_RECHARGE_ID), "Recarga de gás"),
        ]
        self.intake = replace(self.intake, requires_quantity=requires_quantity)
        self.estimated_requirements: list[Any] = []

    async def get_service_intake(self, _: uuid.UUID, __: uuid.UUID):
        return self.intake

    async def estimate(self, _: uuid.UUID, __: uuid.UUID, requirements: Any):
        self.estimated_requirements.append(requirements)
        return self.plan

    async def list_dates(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: Any,
    ) -> tuple[BookingOption, ...]:
        return tuple(self.dates)


def _installation_port(*, requires_quantity: bool = False) -> FakeBookingPort:
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    port.intake = replace(port.intake, requires_quantity=requires_quantity)
    return port


def _equipment_context(**values: object) -> dict[str, object]:
    return {
        "service_id": str(SERVICE_ID),
        "request_mode": "quote",
        **values,
    }


@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa esse serviço?",
        "Demora 2 horas para instalar?",
        "Qual modelo gasta menos energia?",
        "Esse serviço inclui a tubulação?",
    ],
)
@pytest.mark.asyncio
async def test_side_question_never_becomes_equipment_model(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_MODEL,
        context=_equipment_context(
            equipment_ownership="has_equipment",
            equipment_model_known=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(100, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert "equipment_model" not in repository.context
    assert repository.context["service_id"] == str(SERVICE_ID)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("1", 1),
        ("dois", 2),
        ("São 3 aparelhos", 3),
        ("quatro unidades", 4),
        ("preciso limpar uma unidade", 1),
        ("tenho cinco aparelhos", 5),
    ],
)
@pytest.mark.asyncio
async def test_quantity_checkpoint_accepts_only_equipment_count(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context=_equipment_context(),
    )

    await ConversationEngine(
        repository,
        _installation_port(requires_quantity=True),
    ).process(inbound(101, body=body))

    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert repository.state != ConversationState.BOOKING_QUANTITY


@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa? Meu limite é 500 reais",
        "Demora 2 horas?",
        "O modelo é de 12 mil BTUs",
        "A tubulação tem até 3 metros",
        "No ambiente ficam 4 pessoas",
        "Posso pagar em 10 vezes?",
    ],
)
@pytest.mark.asyncio
async def test_incidental_number_is_not_equipment_quantity(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context=_equipment_context(),
    )

    await ConversationEngine(
        repository,
        _installation_port(requires_quantity=True),
    ).process(inbound(102, body=body))

    assert repository.state == ConversationState.BOOKING_QUANTITY
    assert "quantity" not in repository.context
    assert "equipment_quantity" not in repository.context


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("4 pessoas", 4),
        ("no máximo três pessoas", 3),
        ("somos 2", 2),
        ("uma pessoa", 1),
    ],
)
@pytest.mark.asyncio
async def test_people_checkpoint_accepts_typed_occupancy(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(103, body=body)
    )

    assert repository.context["room_people_max"] == expected


@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa? Posso gastar até 2 mil reais",
        "A instalação demora 3 horas?",
        "Meu aparelho atual é de 12 mil BTUs",
        "O endereço é número 40",
    ],
)
@pytest.mark.asyncio
async def test_incidental_number_is_not_room_occupancy(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(104, body=body)
    )

    assert "room_people_max" not in repository.context
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("20 m2", 20),
        ("o quarto tem 16 metros quadrados", 16),
        ("aproximadamente 25m²", 25),
    ],
)
@pytest.mark.asyncio
async def test_area_checkpoint_accepts_typed_area(body: str, expected: int) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
            room_people_max=3,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(105, body=body)
    )

    assert repository.context["room_area_m2"] == expected


@pytest.mark.parametrize(
    "body",
    [
        "O aparelho custa 100 reais por mês?",
        "A visita demora 2 horas?",
        "Quero parcelar em 12 vezes",
    ],
)
@pytest.mark.asyncio
async def test_incidental_number_is_not_room_area(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
            room_people_max=3,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(106, body=body)
    )

    assert "room_area_m2" not in repository.context
    assert repository.context["room_people_max"] == 3


@pytest.mark.parametrize(
    "body",
    [
        "também quero manutenção",
        "quero manutenção também",
    ],
)
@pytest.mark.asyncio
async def test_additional_marker_in_both_positions_preserves_current_service(
    body: str,
) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)
    assert interpretation.has_act(ConversationAct.ADDITIONAL_REQUEST)

    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "service_address": {"address_line": "Rua Antiga, 10"},
        },
    )
    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(107, body=body)
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["pending_service_change_id"] == str(MAINTENANCE_ID)
    assert repository.context["quantity"] == 2


@pytest.mark.asyncio
async def test_service_correction_happens_before_price_answer() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(108, body="na verdade quero manutenção; quanto custa?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.context["service_id"] == str(MAINTENANCE_ID)
    assert "r$ 100,00" in response
    assert "atendimento técnico" in response


@pytest.mark.asyncio
async def test_quantity_correction_happens_before_price_and_duration_answer() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "equipment_quantity": 2,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(
            109,
            body="na verdade são 3 aparelhos; quanto custa e quanto tempo demora?",
        )
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.context["quantity"] == 3
    assert repository.context["equipment_quantity"] == 3
    assert "r$ 100,00" in response
    assert "30 minutos" in response


@pytest.mark.asyncio
async def test_additional_request_is_registered_before_price_answer() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID), "quantity": 1},
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(110, body="também quero manutenção; quanto custa?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["pending_service_change_id"] == str(MAINTENANCE_ID)
    assert "r$ 100,00" in response
    assert "manutenção" in response


@pytest.mark.parametrize(
    ("state", "context", "resume_fragment"),
    [
        (ConversationState.BOOKING_ADDRESS, {"service_id": str(SERVICE_ID)}, "endereço"),
        (ConversationState.BOOKING_DATE, {"service_id": str(SERVICE_ID)}, "data"),
        (
            ConversationState.BOOKING_TIME,
            {"service_id": str(SERVICE_ID), "selected_date": "2026-09-02"},
            "09:00",
        ),
    ],
)
@pytest.mark.asyncio
async def test_visit_fee_question_preserves_checkpoint_without_retry_mutation(
    state: ConversationState,
    context: dict[str, object],
    resume_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    port = MultiServiceBookingPort()
    port.intake = replace(port.intake, requires_address=state is ConversationState.BOOKING_ADDRESS)
    before = dict(repository.context)

    await ConversationEngine(repository, port).process(
        inbound(111, body="A visita técnica é cobrada?")
    )

    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    interpretation = DeterministicConversationInterpreter().interpret(
        "A visita técnica é cobrada?"
    )
    assert interpretation.has(ConversationIntent.PRICE_QUESTION)
    assert interpretation.has_act(ConversationAct.SIDE_QUESTION)
    assert repository.state == state
    assert repository.context == before
    assert "repair_attempts" not in repository.context
    assert "r$ 100,00" not in response
    assert resume_fragment in response

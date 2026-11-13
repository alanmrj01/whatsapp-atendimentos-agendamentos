"""Rodada 2B — correções tardias e invalidação seletiva de contexto."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    confirmation_context,
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

    async def get_service_intake(self, _: uuid.UUID, __: uuid.UUID):
        return self.intake

    async def estimate(self, _: uuid.UUID, __: uuid.UUID, ___: Any):
        return self.plan

    async def list_dates(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: Any,
    ) -> tuple[BookingOption, ...]:
        self.calls.append("dates")
        return tuple(self.dates)


@pytest.mark.asyncio
async def test_late_date_correction_replaces_date_and_invalidates_time() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        customer_name="Marina Cliente",
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-10-14",
            "selected_time": "09:00",
            "candidate_booking": {"selected_time": "09:00"},
            "service_address": OLD_ADDRESS,
            "property_type": "house",
        },
    )
    port = FakeBookingPort()
    port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
        BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
    ]
    port.list_times = AsyncMock(
        return_value=(BookingOption("14:30", "14:30"),)
    )

    await ConversationEngine(repository, port).process(
        inbound(200, body="não é dia 14, é dia 21/10")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-21"
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["property_type"] == "house"


@pytest.mark.asyncio
async def test_late_time_correction_updates_candidate_without_confirming() -> None:
    context = {
        **confirmation_context(),
        "service_address": OLD_ADDRESS,
        "property_type": "house",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=context,
    )
    port = FakeBookingPort()
    port.times = [
        BookingOption("09:00", "09:00"),
        BookingOption("10:00", "10:00"),
    ]

    await ConversationEngine(repository, port).process(
        inbound(201, body="Não é 09:00, é 10:00")
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == "10:00"
    assert repository.context["candidate_booking"]["selected_time"] == "10:00"
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["property_type"] == "house"
    assert "confirm" not in port.calls


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("na verdade são 3 aparelhos", 3),
        ("corrigindo: preciso limpar quatro unidades", 4),
        ("não são 2, são cinco aparelhos", 5),
    ],
)
@pytest.mark.asyncio
async def test_late_quantity_correction_updates_canonical_fact_and_restarts_dates(
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
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(202, body=body)
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.parametrize(
    "body",
    [
        "corrigindo: o endereço é Rua Nova, 25, Centro, Jacareí - SP",
        "na verdade é Avenida Brasil, 300, Jardim Sul, São José dos Campos - SP",
        "o endereço não é o antigo; é Rua das Flores, 48, Centro, Taubaté - SP",
    ],
)
@pytest.mark.asyncio
async def test_late_address_correction_preserves_safe_facts_and_restarts_dates(
    body: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        customer_name="Marina Cliente",
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 2,
            "property_type": "house",
            "onsite_contact_name": "Marina",
            "service_address": OLD_ADDRESS,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(203, body=body)
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["quantity"] == 2
    assert repository.context["property_type"] == "house"
    assert repository.context["onsite_contact_name"] == "Marina"
    assert repository.customer_name == "Marina Cliente"
    assert repository.context["service_address"] != OLD_ADDRESS
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.asyncio
async def test_incomplete_address_correction_stops_using_old_route_and_asks_city() -> None:
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
            "quote_presented": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(206, body="corrigindo, o endereço é Rua Nova, 123")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["pending_service_address"] == "Rua Nova, 123"
    assert repository.context["quantity"] == 2
    assert repository.context["property_type"] == "house"
    assert "service_address" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.asyncio
async def test_contradictory_service_correction_uses_latest_service() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        customer_name="Marina Cliente",
        context={
            "service_id": str(SERVICE_ID),
            "service_address": OLD_ADDRESS,
            "property_type": "house",
            "selected_date": "2026-09-02",
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(204, body="não é limpeza, é manutenção")
    )

    assert repository.context["service_id"] == str(MAINTENANCE_ID)
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["property_type"] == "house"


@pytest.mark.parametrize(
    ("body", "expected_id", "expected_state"),
    [
        (
            "na verdade preciso de recarga de gás",
            GAS_RECHARGE_ID,
            ConversationState.BOOKING_EQUIPMENT_MODEL,
        ),
        (
            "quis dizer instalação de split",
            INSTALLATION_ID,
            ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        ),
        (
            "corrigindo: manutenção",
            MAINTENANCE_ID,
            ConversationState.BOOKING_EQUIPMENT_MODEL,
        ),
    ],
)
@pytest.mark.asyncio
async def test_service_switch_keeps_only_independent_facts(
    body: str,
    expected_id: uuid.UUID,
    expected_state: ConversationState,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        customer_name="Marina Cliente",
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
            "recommended_equipment": {"id": "stale-equipment"},
        },
    )

    await ConversationEngine(repository, MultiServiceBookingPort()).process(
        inbound(205, body=body)
    )

    assert repository.context["service_id"] == str(expected_id)
    assert repository.context["service_address"] == OLD_ADDRESS
    assert repository.context["property_type"] == "house"
    assert repository.context["onsite_contact_name"] == "Marina"
    assert repository.customer_name == "Marina Cliente"
    assert "quantity" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context
    assert "recommended_equipment" not in repository.context
    assert repository.state == expected_state

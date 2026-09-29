from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

from pytest import MonkeyPatch, mark, raises

from app.booking.equipment_recommender import EquipmentCatalogEntry
from app.booking.domain import (
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceEstimate,
    ServiceIntake,
    TravelEstimate,
)

from app.conversations.constants import ConversationState
from app.conversations.engine import (
    ConversationEngine,
    build_outbound_idempotency_key,
)
from app.conversations.ports import (
    BookingConfirmation,
    BookingOption,
    ExistingBooking,
    ServiceDetails,
    SlotUnavailable,
)
from app.conversations.types import (
    ConversationInput,
    ConversationSnapshot,
    ConversationTransition,
)
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.processor import process_webhook_events
from app.whatsapp.webhook import InboundMessageEvent, build_event_key

BUSINESS_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
CUSTOMER_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
CONVERSATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")
SERVICE_ID = uuid.UUID("40000000-0000-0000-0000-000000000004")
APPOINTMENT_ID = uuid.UUID("50000000-0000-0000-0000-000000000005")
EMPLOYEE_ID = uuid.UUID("60000000-0000-0000-0000-000000000006")


@dataclass(frozen=True, slots=True)
class StoredOutbound:
    transition: ConversationTransition
    idempotency_key: str
    status: str = "pending"
    provider_message_id: str | None = None


class FakeConversationRepository:
    def __init__(
        self,
        *,
        state: str = ConversationState.START,
        context: dict[str, Any] | None = None,
        automation_enabled: bool = True,
        handoff_status: str = "none",
        assistant_enabled: bool = True,
        greeting_message: str = "Olá! Como posso ajudar com seu ar-condicionado?",
        fallback_message: str = "Não entendi. Conte em poucas palavras o serviço que você precisa.",
        handoff_message: str = "Seu atendimento foi encaminhado para uma pessoa da equipe.",
        customer_name: str | None = "Cliente",
        business_timezone: str = "America/Sao_Paulo",
    ) -> None:
        self.state = state
        self.context = context or {}
        self.automation_enabled = automation_enabled
        self.handoff_status = handoff_status
        self.assistant_enabled = assistant_enabled
        self.greeting_message = greeting_message
        self.fallback_message = fallback_message
        self.handoff_message = handoff_message
        self.customer_name = customer_name
        self.business_timezone = business_timezone
        self.outbounds: list[StoredOutbound] = []
        self.idempotency_keys: set[str] = set()
        self.lock = asyncio.Lock()
        self.fail_after_outbound = False

    @asynccontextmanager
    async def lock_conversation(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AsyncIterator[ConversationSnapshot]:
        assert business_id == BUSINESS_ID
        assert conversation_id == CONVERSATION_ID
        async with self.lock:
            yield self.snapshot()

    async def outbound_exists(self, idempotency_key: str) -> bool:
        return idempotency_key in self.idempotency_keys

    async def persist_transition(
        self,
        _: ConversationSnapshot,
        transition: ConversationTransition,
        idempotency_key: str,
    ) -> bool:
        if idempotency_key in self.idempotency_keys:
            return False
        self.idempotency_keys.add(idempotency_key)
        self.outbounds.append(StoredOutbound(transition, idempotency_key))
        if self.fail_after_outbound:
            raise RuntimeError("simulated persistence failure")
        self.state = transition.state.value
        self.context = copy.deepcopy(transition.context)
        self.automation_enabled = transition.automation_enabled
        self.handoff_status = transition.handoff_status
        if transition.customer_name is not None:
            self.customer_name = transition.customer_name
        return True

    def snapshot(self) -> ConversationSnapshot:
        return ConversationSnapshot(
            business_id=BUSINESS_ID,
            customer_id=CUSTOMER_ID,
            conversation_id=CONVERSATION_ID,
            state=self.state,
            context=copy.deepcopy(self.context),
            automation_enabled=self.automation_enabled,
            handoff_status=self.handoff_status,
            assistant_enabled=self.assistant_enabled,
            greeting_message=self.greeting_message,
            fallback_message=self.fallback_message,
            handoff_message=self.handoff_message,
            customer_name=self.customer_name,
            business_timezone=self.business_timezone,
        )

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "state": self.state,
                "context": self.context,
                "automation_enabled": self.automation_enabled,
                "handoff_status": self.handoff_status,
                "assistant_enabled": self.assistant_enabled,
                "customer_name": self.customer_name,
                "outbounds": self.outbounds,
                "idempotency_keys": self.idempotency_keys,
            }
        )

    def restore_state(self, state: dict[str, Any]) -> None:
        self.state = state["state"]
        self.context = state["context"]
        self.automation_enabled = state["automation_enabled"]
        self.handoff_status = state["handoff_status"]
        self.assistant_enabled = state["assistant_enabled"]
        self.customer_name = state["customer_name"]
        self.outbounds = state["outbounds"]
        self.idempotency_keys = state["idempotency_keys"]


class FakeBookingPort:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.confirmations: list[tuple[Any, ...]] = []
        self.confirmation_requirements: list[BookingRequirements] = []
        self.services = [BookingOption(str(SERVICE_ID), "Service")]
        self.dates = [BookingOption("2026-09-02", "02/09/2026")]
        self.times = [BookingOption("09:00", "09:00")]
        self.intake = ServiceIntake(
            requires_quantity=False,
            requires_address=False,
            considers_difficult_access=False,
            asks_site_time_limit=False,
            automatic_booking=True,
            pricing_type=PricingType.ESTIMATED,
        )
        self.plan = BookingPlan(
            service=ServiceEstimate(
                estimated_duration_minutes=30,
                estimated_price=Decimal("100.00"),
                pricing_type=PricingType.ESTIMATED,
                requires_human_quote=False,
                applied_rules=("base_duration",),
                qualifier="estimated",
            ),
            travel=TravelEstimate(
                travel_minutes=0,
                distance_km=None,
                source="test",
                method="test",
                estimated=False,
            ),
            travel_before_minutes=0,
            travel_after_minutes=0,
            requires_handoff=False,
        )
        starts_at = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        self.confirmation: BookingConfirmation | None = BookingConfirmation(
            appointment_id=APPOINTMENT_ID,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            employee_id=EMPLOYEE_ID,
        )
        self.slot_unavailable = False
        self.existing_bookings = [
            ExistingBooking(
                appointment_id=APPOINTMENT_ID,
                service_id=SERVICE_ID,
                label="Serviço em 02/09/2026 às 09:00",
                requirements=BookingRequirements(),
            )
        ]
        self.included_tubing_meters = Decimal("3")
        self.extra_tubing_price: Decimal | None = None
        self.business_city: str | None = None
        self.business_state: str | None = None
        self.equipment_catalog = [
            EquipmentCatalogEntry(
                item_id="catalog-gree-9000-cold",
                brand="Gree",
                line="G-Top Auto Inverter",
                capacity_btu=9000,
                segment="cost_benefit",
                cycles=("cold",),
                features=("Wi-Fi",),
                indoor_dimensions_cm={"width": 78.3, "height": 26.0, "depth": 18.5},
                outdoor_dimensions_cm={"width": 42.5, "height": 54.5, "depth": 42.0},
                condenser_form="compact",
                image_url="https://example.com/gree-9000.jpg",
                source_url="https://gree.com.br/",
                price=2500.0,
            ),
            EquipmentCatalogEntry(
                item_id="catalog-tcl-9000-heat-cool",
                brand="TCL",
                line="A2 Inverter Quente/Frio",
                capacity_btu=9000,
                segment="cost_benefit",
                cycles=("cold", "heat_cool"),
                features=(),
                indoor_dimensions_cm={"width": 78.0, "height": 27.0, "depth": 20.0},
                outdoor_dimensions_cm={"width": 45.0, "height": 55.0, "depth": 40.0},
                condenser_form="compact",
                image_url=None,
                source_url="https://example.com/tcl",
                price=2600.0,
            ),
        ]

    async def list_services(self, _: uuid.UUID) -> tuple[BookingOption, ...]:
        self.calls.append("services")
        return tuple(self.services)

    async def get_service_details(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
    ) -> ServiceDetails:
        assert service_id == SERVICE_ID
        service = next(
            (item for item in self.services if item.id == str(service_id)),
            BookingOption(str(service_id), "Service"),
        )
        return ServiceDetails(
            id=service_id,
            name=service.label,
            description="Serviço configurado para teste.",
            included_tubing_meters=self.included_tubing_meters,
            extra_tubing_price=self.extra_tubing_price,
            business_city=self.business_city,
            business_state=self.business_state,
        )

    async def get_service_intake(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
    ) -> ServiceIntake:
        assert service_id == SERVICE_ID
        return self.intake

    async def list_equipment_catalog(
        self,
        _: uuid.UUID,
    ) -> tuple[EquipmentCatalogEntry, ...]:
        return tuple(self.equipment_catalog)

    async def estimate(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
        __: BookingRequirements,
    ) -> BookingPlan:
        assert service_id == SERVICE_ID
        return self.plan

    async def list_dates(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
        __: BookingRequirements = BookingRequirements(),
    ) -> tuple[BookingOption, ...]:
        assert service_id == SERVICE_ID
        self.calls.append("dates")
        return tuple(self.dates)

    async def list_times(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        __: BookingRequirements = BookingRequirements(),
    ) -> tuple[BookingOption, ...]:
        assert service_id == SERVICE_ID
        assert selected_date == "2026-09-02"
        self.calls.append("times")
        return tuple(self.times)

    async def confirm(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        selected_time: str,
        _: BookingRequirements = BookingRequirements(),
    ) -> BookingConfirmation | None:
        self.calls.append("confirm")
        self.confirmations.append(
            (
                business_id,
                customer_id,
                service_id,
                selected_date,
                selected_time,
            )
        )
        self.confirmation_requirements.append(_)
        if self.slot_unavailable:
            raise SlotUnavailable("slot is no longer available")
        return self.confirmation

    async def list_customer_bookings(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
    ) -> tuple[ExistingBooking, ...]:
        self.calls.append("existing_bookings")
        return tuple(self.existing_bookings)

    async def cancel_booking(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: uuid.UUID,
    ) -> BookingConfirmation:
        self.calls.append("cancel_booking")
        assert self.confirmation is not None
        return self.confirmation

    async def reschedule_booking_atomic(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        ___: uuid.UUID,
        ____: str,
        _____: str,
        ______: BookingRequirements,
    ) -> BookingConfirmation:
        self.calls.append("reschedule_booking")
        assert self.confirmation is not None
        return self.confirmation


def inbound(
    sequence: int,
    *,
    action: str | None = None,
    body: str | None = None,
    whatsapp_id: str | None = None,
    message_type: str | None = None,
) -> ConversationInput:
    return ConversationInput(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        provider_message_id=f"provider-{sequence}",
        message_type=message_type or ("interactive" if action else "text"),
        body=body,
        interactive_id=action,
        whatsapp_id=whatsapp_id,
    )


@mark.asyncio
async def test_global_assistant_switch_stops_outbound_without_losing_inbound_flow() -> None:
    disabled = FakeConversationRepository(assistant_enabled=False)
    enabled = FakeConversationRepository()

    assert await ConversationEngine(disabled, FakeBookingPort()).process(
        inbound(1, body="bom dia")
    ) is False
    assert disabled.outbounds == []

    assert await ConversationEngine(enabled, FakeBookingPort()).process(
        inbound(1, body="bom dia")
    ) is True
    assert enabled.outbounds[0].transition.outbound.message_type == "text"
    assert enabled.outbounds[0].transition.outbound.outbound_payload is None


@mark.asyncio
async def test_natural_service_request_advances_without_permission_question() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="Quero limpar meu ar")
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["equipment_ownership"] == "has_equipment"
    body = repository.outbounds[-1].transition.outbound.body or ""
    assert "marca e o modelo" in body.casefold()
    assert "pode me mandar uma foto" in body.casefold()
    assert "responda" not in body.casefold()
    assert "Quer que eu" not in body


@mark.asyncio
async def test_book_request_while_choosing_service_does_not_loop_menu_copy() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="quero agendar")
    )

    assert repository.state == ConversationState.BOOKING_SERVICE
    body = repository.outbounds[-1].transition.outbound.body or ""
    assert "Qual serviço você quer agendar?" in body
    assert "Ver opções" not in body


@mark.asyncio
async def test_probable_diagnostic_asks_only_for_required_address() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Diagnóstico / manutenção corretiva")
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="Boa tarde, meu ar não está gelando")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    response = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "endereço" in response
    assert "compressor" not in response
    assert "causa" not in response


@mark.asyncio
async def test_natural_handoff_is_direct_and_uses_configured_message() -> None:
    repository = FakeConversationRepository(
        handoff_message="Uma pessoa da equipe continuará por aqui."
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(1, body="Quero falar com uma pessoa")
    )

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.outbounds[-1].transition.outbound.body == (
        "Uma pessoa da equipe continuará por aqui."
    )


@mark.asyncio
async def test_many_available_dates_asks_weekday_before_listing_dates() -> None:
    repository = FakeConversationRepository(state=ConversationState.BOOKING_SERVICE)
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption(
            f"2026-10-{day:02d}",
            f"data {day:02d}/10",
        )
        for day in range(1, 16)
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, action=f"service:{SERVICE_ID}")
    )

    transition = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_WEEKDAY
    messages = (transition.outbound, *transition.follow_ups)
    outbound = next(
        message
        for message in messages
        if message.interactive_id == "booking.weekdays"
    )
    rows = outbound.outbound_payload["sections"][0]["rows"]
    assert 1 <= len(rows) <= 7
    assert all(row["id"].startswith("weekday:") for row in rows)
    assert "dia da semana" in (outbound.body or "").casefold()
    assert booking_port.calls.count("times") == 0


@mark.asyncio
async def test_weekday_choice_narrows_dates_then_selected_date_offers_times() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption("2026-09-24", "quinta, 24 de setembro"),
        BookingOption("2026-10-01", "quinta, 1 de outubro"),
        BookingOption("2026-10-08", "quinta, 8 de outubro"),
        BookingOption("2026-09-25", "sexta, 25 de setembro"),
    ]
    booking_port.list_times = AsyncMock(
        return_value=(
            BookingOption("10:30", "10:30"),
            BookingOption("11:00", "11:00"),
            BookingOption("11:30", "11:30"),
        )
    )

    engine = ConversationEngine(repository, booking_port)
    await engine.process(inbound(1, body="quinta"))

    assert repository.state == ConversationState.BOOKING_DATE
    date_rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in date_rows] == [
        "date:2026-09-24",
        "date:2026-10-01",
        "date:2026-10-08",
    ]

    await engine.process(inbound(2, body="24/09"))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-09-24"
    assert "10:30" in (repository.outbounds[-1].transition.outbound.body or "")


@mark.asyncio
async def test_time_list_never_exceeds_whatsapp_limit_and_keeps_body_compact() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.times = [
        BookingOption(
            f"{8 + index // 2:02d}:{(index % 2) * 30:02d}",
            f"{8 + index // 2:02d}:{(index % 2) * 30:02d}",
        )
        for index in range(16)
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="quais horários?")
    )

    outbound = repository.outbounds[-1].transition.outbound
    rows = outbound.outbound_payload["sections"][0]["rows"]
    body = outbound.body or ""
    assert len(rows) == 10
    assert "entre 08:00 e 15:30" in body
    assert "08:30, 09:00, 09:30" not in body
    assert "Ver opções" in body


@mark.asyncio
async def test_duration_question_during_time_selection_answers_and_resumes_slot() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="Quanto tempo demora a limpeza?")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "30 minutos" in body
    assert "09:00" in body


@mark.asyncio
async def test_question_while_waiting_for_address_is_not_saved_as_address() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(booking_port.intake, requires_address=True)

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="Quanto custa esse serviço?")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "service_address" not in repository.context
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "r$ 100,00" in body
    assert "endereço" in body


@mark.asyncio
async def test_unnamed_customer_is_asked_once_and_pending_service_is_resumed() -> None:
    repository = FakeConversationRepository(customer_name=None)
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(1, body="Olá, bom dia. Preciso de uma limpeza."))

    assert repository.state == ConversationState.CUSTOMER_NAME
    first_body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "qual é o seu nome" in first_body
    assert "não entendi" not in first_body

    await engine.process(inbound(2, body="Alan"))

    assert repository.customer_name == "Alan"
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "endereço" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@mark.asyncio
async def test_customer_can_choose_offered_date_and_time_in_one_text_message() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption("2026-09-02", "quarta, 2 de setembro")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1, body="quarta às 9")
    )

    assert repository.state == ConversationState.BOOKING_ATTENDEE
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == "09:00"


@mark.asyncio
async def test_full_booking_flow_persists_canonical_states_and_context() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, body="olá")) is True
    assert repository.state == ConversationState.MENU

    assert await engine.process(inbound(2, action="menu.book")) is True
    assert repository.state == ConversationState.BOOKING_SERVICE

    assert await engine.process(
        inbound(3, action=f"service:{SERVICE_ID}")
    ) is True
    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context == {"service_id": str(SERVICE_ID)}

    assert await engine.process(
        inbound(4, action="date:2026-09-02")
    ) is True
    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context == {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
    }

    assert await engine.process(
        inbound(5, action="time:09:00", whatsapp_id="5512981359722")
    ) is True
    assert repository.state == ConversationState.BOOKING_ATTENDEE
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == "09:00"

    assert await engine.process(
        inbound(
            6,
            action="attendee.customer",
            body="Sim",
            whatsapp_id="5512981359722",
        )
    ) is True
    assert repository.state == ConversationState.BOOKING_PHONE_CONFIRM

    assert await engine.process(
        inbound(
            7,
            action="phone.confirm",
            body="Sim",
            whatsapp_id="5512981359722",
        )
    ) is True
    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context["contact_phone"] == "+5512981359722"
    assert repository.context["contact_phone_confirmed"] is True

    assert await engine.process(inbound(8, action="booking.confirm")) is True
    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert repository.context == {}

    assert await engine.process(
        inbound(9, action="post_booking.help.no", body="Não, obrigado")
    ) is True
    assert repository.state == ConversationState.COMPLETED
    assert repository.context == {}
    assert booking_port.confirmations == [
        (
            BUSINESS_ID,
            CUSTOMER_ID,
            SERVICE_ID,
            "2026-09-02",
            "09:00",
        )
    ]
    messages = [
        message
        for stored in repository.outbounds
        for message in (stored.transition.outbound, *stored.transition.follow_ups)
    ]
    interactive_ids = [
        message.interactive_id
        for message in messages
        if message.interactive_id is not None
    ]
    assert interactive_ids == [
        "booking.services",
        "booking.dates",
        "booking.times",
        "booking.attendee",
        "booking.phone_confirmation",
        "booking.confirmation",
        "post_booking.help",
    ]
    interactive = {
        message.interactive_id: message
        for message in messages
        if message.interactive_id is not None
    }
    assert interactive["booking.services"].outbound_payload["sections"][0]["rows"] == [
        {"id": f"service:{SERVICE_ID}", "title": "Service"}
    ]
    assert interactive["booking.dates"].outbound_payload["sections"][0]["rows"] == [
        {"id": "date:2026-09-02", "title": "02/09/2026"}
    ]
    assert interactive["booking.times"].outbound_payload["sections"][0]["rows"] == [
        {"id": "time:09:00", "title": "09:00"}
    ]
    assert [
        button["id"]
        for button in interactive["booking.attendee"].outbound_payload["buttons"]
    ] == ["attendee.customer", "attendee.other"]
    assert [
        button["id"]
        for button in interactive["booking.phone_confirmation"].outbound_payload["buttons"]
    ] == ["phone.confirm", "phone.other"]
    assert [
        button["id"]
        for button in interactive["booking.confirmation"].outbound_payload["buttons"]
    ] == ["booking.confirm", "booking.back", "booking.cancel"]


@mark.asyncio
async def test_outbound_payload_is_snapshot_of_booking_options() -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    booking_port = FakeBookingPort()
    booking_port.services = [BookingOption(str(SERVICE_ID), "Original")]
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="menu.book")) is True
    stored_payload = copy.deepcopy(
        repository.outbounds[0].transition.outbound.outbound_payload
    )

    booking_port.services[0] = BookingOption(str(SERVICE_ID), "Changed")

    stored_outbound = repository.outbounds[0].transition.outbound
    assert stored_outbound.outbound_payload == stored_payload
    assert stored_payload["sections"][0]["rows"][0]["title"] == "Original"


@mark.parametrize(
    ("state", "context", "empty_collection", "action", "expected_state"),
    [
        (
            ConversationState.MENU,
            {},
            "services",
            "menu.book",
            ConversationState.MENU,
        ),
        (
            ConversationState.BOOKING_SERVICE,
            {},
            "dates",
            f"service:{SERVICE_ID}",
            ConversationState.BOOKING_SERVICE,
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "times",
            "date:2026-09-02",
            ConversationState.BOOKING_DATE,
        ),
    ],
)
@mark.asyncio
async def test_empty_availability_does_not_advance_to_impossible_choice(
    state: ConversationState,
    context: dict[str, Any],
    empty_collection: str,
    action: str,
    expected_state: ConversationState,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    setattr(booking_port, empty_collection, [])
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert len(repository.outbounds) == 1


@mark.parametrize(
    ("state", "context", "action"),
    [
        (ConversationState.MENU, {}, "menu.book"),
        (ConversationState.BOOKING_SERVICE, {}, f"service:{SERVICE_ID}"),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "date:2026-09-02",
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
            "time:09:00",
        ),
        (
            ConversationState.BOOKING_CONFIRM,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
                "selected_time": "09:00",
            },
            "booking.confirm",
        ),
    ],
)
@mark.asyncio
async def test_missing_booking_port_is_fail_closed(
    state: ConversationState,
    context: dict[str, Any],
    action: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    engine = ConversationEngine(repository, booking_port=None)

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == state
    assert repository.state != ConversationState.COMPLETED
    assert repository.context == context
    assert repository.outbounds[0].transition.outbound.message_type == "text"
    assert repository.outbounds[0].transition.outbound.outbound_payload is None


def confirmation_context() -> dict[str, Any]:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    return {**candidate, "candidate_booking": candidate}


@mark.asyncio
async def test_invalid_confirmation_result_does_not_complete_booking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
    )
    booking_port = FakeBookingPort()
    booking_port.confirmation = None
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="booking.confirm")) is True

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context == confirmation_context()


@mark.asyncio
async def test_slot_unavailable_does_not_complete_booking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
    )
    booking_port = FakeBookingPort()
    booking_port.slot_unavailable = True
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="booking.confirm")) is True

    assert repository.state == ConversationState.BOOKING_CONFIRM
    payload = repository.outbounds[0].transition.outbound.outbound_payload
    assert [button["id"] for button in payload["buttons"]] == [
        "booking.back",
        "booking.cancel",
    ]


@mark.parametrize(
    ("action", "expected_state"),
    [
        ("menu.reschedule", ConversationState.RESCHEDULE),
        ("menu.cancel", ConversationState.CANCEL),
    ],
)
@mark.asyncio
async def test_menu_secondary_routes(
    action: str,
    expected_state: ConversationState,
) -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert repository.context == {}


@mark.asyncio
async def test_handoff_creates_last_outbound_then_disables_automation() -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action="menu.human")) is True
    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"
    assert len(repository.outbounds) == 1

    assert await engine.process(inbound(2, body="any later message")) is False
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_disabled_automation_does_not_change_state_or_create_outbound() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        context={"service_id": str(SERVICE_ID)},
        automation_enabled=False,
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action="menu.book")) is False

    assert repository.state == ConversationState.MENU
    assert repository.context == {"service_id": str(SERVICE_ID)}
    assert repository.outbounds == []


@mark.parametrize(
    ("state", "context", "action"),
    [
        (ConversationState.MENU, {}, "unknown.action"),
        (ConversationState.BOOKING_SERVICE, {}, "service:not-a-uuid"),
        (
            ConversationState.BOOKING_SERVICE,
            {},
            "service:50000000-0000-0000-0000-000000000005",
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "date:invalid",
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
            "time:invalid",
        ),
    ],
)
@mark.asyncio
async def test_unexpected_or_invalid_ids_do_not_advance_state(
    state: ConversationState,
    context: dict[str, Any],
    action: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == state
    assert repository.context == context
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_completed_conversation_returns_to_menu_on_new_message() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        context={"selected_time": "must-be-cleared"},
    )
    engine = ConversationEngine(repository)

    assert await engine.process(inbound(1, body="oi novamente")) is True

    assert repository.state == ConversationState.MENU
    assert repository.context == {}


@mark.parametrize(
    ("action", "expected_state", "expected_context"),
    [
        (
            "booking.back",
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
        ),
        ("booking.cancel", ConversationState.MENU, {}),
    ],
)
@mark.asyncio
async def test_booking_confirmation_navigation_uses_stable_ids(
    action: str,
    expected_state: ConversationState,
    expected_context: dict[str, Any],
) -> None:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={**candidate, "candidate_booking": candidate},
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert repository.context == expected_context


@mark.asyncio
async def test_booking_collects_only_simple_required_information() -> None:
    repository = FakeConversationRepository(state=ConversationState.BOOKING_SERVICE)
    booking_port = FakeBookingPort()
    booking_port.intake = ServiceIntake(
        requires_quantity=True,
        requires_address=True,
        considers_difficult_access=True,
        asks_site_time_limit=True,
        automatic_booking=True,
        pricing_type=PricingType.ESTIMATED,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(1, action=f"service:{SERVICE_ID}"))
    assert repository.state == ConversationState.BOOKING_QUANTITY
    await engine.process(inbound(2, action="quantity:3"))
    assert repository.state == ConversationState.BOOKING_ACCESS
    await engine.process(inbound(3, action="access.unknown"))
    assert repository.state == ConversationState.BOOKING_ADDRESS
    await engine.process(
        inbound(4, body="Rua das Flores, 10, São José dos Campos - SP")
    )
    assert repository.state == ConversationState.BOOKING_SITE_LIMIT
    await engine.process(inbound(5, action="site_limit.17:00"))

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["quantity"] == 3
    assert repository.context["access_condition"] == "unknown"
    assert repository.context["site_allowed_end"] == "17:00"
    questions = " ".join(
        item.transition.outbound.body or "" for item in repository.outbounds
    ).casefold()
    assert "quantos itens ou aparelhos" in questions
    assert "endereço completo" in questions
    assert "horário limite" in questions
    for forbidden in (
        "linha frigorígena",
        "desnível",
        "bitola",
        "tipo de fluido",
        "carga térmica",
    ):
        assert forbidden not in questions


@mark.asyncio
async def test_human_quote_service_goes_directly_to_handoff() -> None:
    repository = FakeConversationRepository(state=ConversationState.BOOKING_SERVICE)
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        automatic_booking=False,
        pricing_type=PricingType.HUMAN_QUOTE,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(1, action=f"service:{SERVICE_ID}")
    )

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"


@mark.asyncio
async def test_unconfigured_free_text_request_asks_for_service_without_handoff() -> None:
    repository = FakeConversationRepository(state=ConversationState.BOOKING_SERVICE)

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(1, body="Quero comprar uma peça")
    )

    assert repository.state == ConversationState.BOOKING_SERVICE
    assert repository.automation_enabled is True


@mark.asyncio
async def test_fixed_and_estimated_prices_use_plain_language() -> None:
    for pricing_type, expected in (
        (PricingType.FIXED, "O valor do serviço é R$ 100,00."),
        (PricingType.ESTIMATED, "o valor estimado é R$ 100,00"),
    ):
        repository = FakeConversationRepository(
            state=ConversationState.BOOKING_SERVICE
        )
        booking_port = FakeBookingPort()
        booking_port.intake = replace(
            booking_port.intake,
            pricing_type=pricing_type,
        )
        booking_port.plan = replace(
            booking_port.plan,
            service=replace(
                booking_port.plan.service,
                pricing_type=pricing_type,
            ),
        )

        await ConversationEngine(repository, booking_port).process(
            inbound(1, action=f"service:{SERVICE_ID}")
        )

        assert expected in repository.outbounds[-1].transition.outbound.body


@mark.asyncio
async def test_same_inbound_is_idempotent_with_deterministic_unique_key() -> None:
    repository = FakeConversationRepository()
    engine = ConversationEngine(repository)
    message = inbound(1, body="hello")

    assert await engine.process(message) is True
    assert await engine.process(message) is False

    assert len(repository.outbounds) == 1
    stored = repository.outbounds[0]
    assert stored.idempotency_key == build_outbound_idempotency_key(message)
    assert stored.idempotency_key == build_outbound_idempotency_key(message)
    assert stored.status == "pending"
    assert stored.provider_message_id is None


@mark.asyncio
async def test_concurrent_same_conversation_is_serialized_and_idempotent() -> None:
    repository = FakeConversationRepository()
    engine = ConversationEngine(repository)
    message = inbound(1, body="concurrent")

    results = await asyncio.gather(
        engine.process(message),
        engine.process(message),
    )

    assert sorted(results) == [False, True]
    assert repository.state == ConversationState.MENU
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_engine_never_calls_whatsapp_client(
    monkeypatch: MonkeyPatch,
) -> None:
    client_methods = (
        "send_text",
        "send_interactive_buttons",
        "send_interactive_list",
        "mark_as_read",
    )
    mocks: list[AsyncMock] = []
    for method_name in client_methods:
        method_mock = AsyncMock(side_effect=AssertionError("Meta call is forbidden"))
        monkeypatch.setattr(WhatsAppClient, method_name, method_mock)
        mocks.append(method_mock)

    repository = FakeConversationRepository()
    assert await ConversationEngine(repository).process(
        inbound(1, body="hello")
    ) is True

    for method_mock in mocks:
        method_mock.assert_not_awaited()


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.inbounds: list[InboundMessageEvent] = []
        self.completed: list[tuple[str, str]] = []

    async def claim_event(self, event: InboundMessageEvent) -> bool:
        if event.event_key in self.claimed:
            return False
        self.claimed.add(event.event_key)
        return True

    async def find_business_id(self, _: str) -> uuid.UUID:
        return BUSINESS_ID

    async def get_or_create_customer_id(self, _: uuid.UUID, __: str) -> uuid.UUID:
        return CUSTOMER_ID

    async def get_or_create_conversation_id(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
    ) -> uuid.UUID:
        return CONVERSATION_ID

    async def touch_conversation(self, _: uuid.UUID) -> None:
        return None

    async def persist_inbound_message(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        event: InboundMessageEvent,
    ) -> None:
        self.inbounds.append(event)

    async def update_message_status(self, *_: Any) -> None:
        return None

    async def complete_event(self, event_key: str, event_status: str) -> None:
        self.completed.append((event_key, event_status))

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "claimed": self.claimed,
                "inbounds": self.inbounds,
                "completed": self.completed,
            }
        )

    def restore_state(self, state: dict[str, Any]) -> None:
        self.claimed = state["claimed"]
        self.inbounds = state["inbounds"]
        self.completed = state["completed"]


class FakeTransactionSession:
    def __init__(self, *stores: Any) -> None:
        self.stores = stores

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        snapshots = [store.export_state() for store in self.stores]
        try:
            yield
        except Exception:
            for store, snapshot in zip(self.stores, snapshots, strict=True):
                store.restore_state(snapshot)
            raise


def webhook_event() -> InboundMessageEvent:
    provider_message_id = "provider-webhook-engine"
    return InboundMessageEvent(
        event_key=build_event_key("inbound", provider_message_id),
        event_type="message.inbound.text",
        meta_phone_number_id="known-phone-id",
        provider_message_id=provider_message_id,
        whatsapp_id="5511999990009",
        message_type="text",
        body="hello",
        interactive_id=None,
    )


@mark.asyncio
async def test_webhook_persists_inbound_and_outbox_in_same_transaction() -> None:
    event_repository = FakeWebhookRepository()
    conversation_repository = FakeConversationRepository()
    session = FakeTransactionSession(event_repository, conversation_repository)
    engine = ConversationEngine(conversation_repository)
    event = webhook_event()

    await process_webhook_events(
        session,
        [event],
        event_repository,
        engine,
    )

    assert event_repository.inbounds == [event]
    assert conversation_repository.state == ConversationState.MENU
    assert len(conversation_repository.outbounds) == 1
    assert event_repository.completed == [(event.event_key, "processed")]


@mark.asyncio
async def test_transaction_rollback_keeps_state_and_outbox_consistent() -> None:
    event_repository = FakeWebhookRepository()
    conversation_repository = FakeConversationRepository()
    conversation_repository.fail_after_outbound = True
    session = FakeTransactionSession(event_repository, conversation_repository)
    engine = ConversationEngine(conversation_repository)

    with raises(RuntimeError, match="simulated persistence failure"):
        await process_webhook_events(
            session,
            [webhook_event()],
            event_repository,
            engine,
        )

    assert event_repository.claimed == set()
    assert event_repository.inbounds == []
    assert event_repository.completed == []
    assert conversation_repository.state == ConversationState.START
    assert conversation_repository.outbounds == []
    assert conversation_repository.idempotency_keys == set()



@mark.asyncio
async def test_stale_booking_button_is_never_saved_as_customer_name() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.CUSTOMER_NAME,
        customer_name=None,
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(101, action="booking.back", body="Voltar")
    )

    assert repository.customer_name is None
    assert repository.state == ConversationState.CUSTOMER_NAME
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "etapa anterior" not in body
    assert "qual é o seu nome" in body
    assert "nome" in body
    assert "prazer, voltar" not in body


@mark.asyncio
async def test_explicit_name_can_correct_previous_bad_capture_and_keep_request() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        customer_name="Voltar",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(
            102,
            body="Me chamo Alan, preciso de uma instalação de ar condicionado",
        )
    )

    assert repository.customer_name == "Alan"
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "endereço" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@mark.asyncio
async def test_tubing_prompt_is_split_into_two_real_outbox_messages() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"

    await ConversationEngine(repository, booking_port).process(
        inbound(
            103,
            body="Rua Maurício Cardoso, 201 - Jardim Sul, São José dos Campos",
        )
    )

    transition = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_TUBING
    assert "quantos metros" in (transition.outbound.body or "").casefold()
    assert "responda" not in (transition.outbound.body or "").casefold()
    assert len(transition.follow_ups) == 1
    guidance = (transition.follow_ups[0].body or "").casefold()
    assert "3 metros" in guidance
    assert "técnico" in guidance
    assert "valor pode variar" in guidance


@mark.asyncio
async def test_uncertain_tubing_value_is_confirmed_before_advancing() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TUBING,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        asks_tubing_length=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(104, body="Acredito ue 3 metros sejam difícil")
    )

    assert repository.state == ConversationState.BOOKING_TUBING
    assert repository.context["pending_tubing_meters"] == "3"
    assert repository.automation_enabled is True
    confirmation = repository.outbounds[-1].transition.outbound
    assert confirmation.message_type == "interactive_button"
    assert "3 metros" in (confirmation.body or "").casefold()

    await engine.process(
        inbound(105, action="tubing.confirm", body="Sim")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["tubing_meters"] == "3"
    assert repository.context["tubing_length_answered"] is True
    assert repository.automation_enabled is True


@mark.asyncio
async def test_unknown_tubing_keeps_automatic_flow_without_inventing_measurement() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TUBING,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        asks_tubing_length=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(106, body="Não tenho certeza")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["tubing_length_answered"] is True
    assert "tubing_meters" not in repository.context
    assert repository.automation_enabled is True
    assert repository.handoff_status == "none"



@mark.asyncio
async def test_bodyless_unsupported_inbound_is_persisted_but_not_answered() -> None:
    event_repository = FakeWebhookRepository()
    conversation_repository = FakeConversationRepository(customer_name=None)
    session = FakeTransactionSession(event_repository, conversation_repository)
    engine = AsyncMock()
    provider_message_id = "provider-unsupported"
    event = InboundMessageEvent(
        event_key=build_event_key("inbound", provider_message_id),
        event_type="message.inbound.image",
        meta_phone_number_id="known-phone-id",
        provider_message_id=provider_message_id,
        whatsapp_id="5511999990009",
        message_type="image",
        body=None,
        interactive_id=None,
    )

    await process_webhook_events(
        session,
        [event],
        event_repository,
        engine,
    )

    assert event_repository.inbounds == [event]
    assert event_repository.completed == [(event.event_key, "ignored")]
    engine.process.assert_not_awaited()



@mark.asyncio
async def test_travel_handoff_explains_reason_instead_of_abrupt_generic_message() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TUBING,
        context={
            "service_id": str(SERVICE_ID),
            "service_address": {
                "address_line": "Rua Maurício Cardoso, 201 - Jardim Sul"
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )
    booking_port.plan = replace(
        booking_port.plan,
        requires_handoff=True,
        handoff_reason="travel_estimate_unavailable",
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(107, body="3 metros")
    )

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    body = (repository.outbounds[-1].transition.outbound.body or "").casefold()
    assert "deslocamento" in body
    assert "endereço" in body
    assert "confirmar" in body



@mark.asyncio
async def test_incomplete_address_asks_city_directly_before_route_planning() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(
            201,
            body="Rua Maurício Cardoso, 201\nJardim Sul",
        )
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["pending_service_address"].startswith(
        "Rua Maurício Cardoso"
    )
    assert repository.context["awaiting_address_city"] is True
    prompt = repository.outbounds[-1].transition.outbound
    assert prompt.message_type == "text"
    assert prompt.interactive_id is None
    body = (prompt.body or "").casefold()
    assert "apenas a cidade" in body
    assert "são josé dos campos" not in body

@mark.asyncio
async def test_customer_can_supply_different_city_after_city_confirmation() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "pending_service_address": "Rua das Flores, 10 - Centro",
            "pending_address_city_guess": "São José dos Campos - SP",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(
            203,
            action="address.city.other",
            body="Outra cidade",
        )
    )
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["awaiting_address_city"] is True

    await engine.process(inbound(204, body="Jacareí - SP"))

    assert repository.state == ConversationState.BOOKING_DATE
    address = repository.context["service_address"]
    assert address["city"] == "Jacareí - SP"



@mark.asyncio
async def test_greeting_with_tudo_bem_mirrors_customer_tone() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        customer_name="Alan",
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(301, body="bom dia, tudo bem?")
    )

    body = repository.outbounds[-1].transition.outbound.body or ""
    assert body.startswith("Bom dia, Alan!")
    assert "Tudo bem, e com você?" in body
    assert "Como posso te ajudar?" in body


@mark.asyncio
async def test_greeting_plus_request_is_not_lost_while_asking_name() -> None:
    repository = FakeConversationRepository(customer_name=None)
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(
            302,
            body="Oi, boa tarde! Tudo bem? Preciso de uma higienização no meu ar condicionado",
        )
    )

    first = repository.outbounds[-1].transition.outbound.body or ""
    assert repository.state == ConversationState.CUSTOMER_NAME
    assert "Oi, boa tarde!" in first
    assert "Tudo bem, e com você?" in first
    assert "qual é o seu nome" in first.casefold()
    assert "Como posso te ajudar?" not in first

    await engine.process(inbound(303, body="Alan"))

    assert repository.customer_name == "Alan"
    assert repository.state == ConversationState.BOOKING_ADDRESS
    second = repository.outbounds[-1].transition.outbound.body or ""
    assert second.startswith("Prazer, Alan.")
    assert "endereço" in second.casefold()
    assert "qual serviço" not in second.casefold()


@mark.asyncio
async def test_purchase_request_is_clarified_instead_of_generic_service_list() -> None:
    repository = FakeConversationRepository(customer_name=None)
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(
            304,
            body="bom dia! gostaria de comprar um ar condiciionado para o meu quarto",
        )
    )
    assert repository.state == ConversationState.CUSTOMER_NAME

    await engine.process(inbound(305, body="joao"))

    outbound = repository.outbounds[-1].transition.outbound
    assert repository.state == ConversationState.BOOKING_SERVICE
    assert outbound.message_type == "interactive_button"
    body = (outbound.body or "").casefold()
    assert "comprar o aparelho" in body
    assert "instalação" in body
    assert "como posso" not in body
    buttons = [button["id"] for button in outbound.outbound_payload["buttons"]]
    assert buttons == [
        "equipment.installation",
        "equipment.purchase",
        "equipment.both",
    ]


@mark.asyncio
async def test_purchase_clarification_installation_choice_resumes_booking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={"service_clarification": "equipment_purchase"},
        customer_name="Joao",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(
            306,
            action="equipment.installation",
            body="Só instalação",
        )
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "service_clarification" not in repository.context
    assert "endereço" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@mark.asyncio
async def test_abbreviated_address_with_different_city_does_not_force_business_city() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Joao",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"

    await ConversationEngine(repository, booking_port).process(
        inbound(307, body="rua 28 pq imperial jacarei")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    stored = repository.context["service_address"]["address_line"]
    assert "Parque imperial jacarei" in stored
    assert "São José dos Campos" not in stored


@mark.asyncio
async def test_city_reply_after_other_city_is_accepted_without_reasking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "pending_service_address": "Rua 28, número 47, Parque Imperial",
            "awaiting_address_city": True,
            "repair_attempts": {"city": 1},
        },
        customer_name="Joao",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"

    await ConversationEngine(repository, booking_port).process(
        inbound(308, body="jacarei")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    address = repository.context["service_address"]
    assert address["city"] == "jacarei"
    assert "repair_attempts" not in repository.context


@mark.asyncio
async def test_same_city_question_is_never_asked_more_than_twice() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "pending_service_address": "Rua 28, 47, Parque Imperial",
            "awaiting_address_city": True,
            "repair_attempts": {"city": 1},
        },
        customer_name="Joao",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(309, body="não faço ideia")
    )

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert "com segurança" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@mark.asyncio
async def test_invalid_quantity_is_rephrased_once_then_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_quantity=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(310, body="não sei"))
    assert repository.state == ConversationState.BOOKING_QUANTITY
    assert repository.context["repair_attempts"]["quantity"] == 1

    await engine.process(inbound(311, body="complicado"))
    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False



@mark.asyncio
async def test_installation_quote_collects_equipment_before_height_and_never_asks_tubing_length() -> None:
    repository = FakeConversationRepository(customer_name="Alan")
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(401, body="Preciso de uma cotação para instalação de ar condicionado")
    )
    assert repository.state == ConversationState.BOOKING_ADDRESS

    await engine.process(
        inbound(
            402,
            body="Rua das Flores, 20, Centro, São José dos Campos - SP",
        )
    )
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_OWNERSHIP

    await engine.process(
        inbound(403, action="equipment.has", body="Já tenho")
    )
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL

    await engine.process(inbound(404, body="LG Dual Inverter 12000 BTU"))
    assert repository.state == ConversationState.BOOKING_INSTALLATION_HEIGHT

    await engine.process(
        inbound(405, action="height.at_most_3m", body="Até 3 metros")
    )
    transition = repository.outbounds[-1].transition
    combined = " ".join(
        [
            transition.outbound.body or "",
            *(item.body or "" for item in transition.follow_ups),
        ]
    ).casefold()
    assert repository.state == ConversationState.BOOKING_PROPERTY
    assert "tubulação" in combined
    assert "quantos metros" not in combined
    assert repository.context["tubing_length_answered"] is True


@mark.asyncio
async def test_equipment_recommendation_waits_for_all_three_profile_answers() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "service_address": {
                "address_line": "Rua A, 10",
                "city": "São José dos Campos",
                "state": "SP",
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(406, body="No máximo 3 pessoas"))
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "recommended_equipment" not in repository.context

    await engine.process(inbound(407, body="O ambiente tem 16 m2"))
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "recommended_equipment" not in repository.context

    await engine.process(inbound(408, body="Quero algo mais moderno"))
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "recommended_equipment" not in repository.context

    await engine.process(inbound(409, body="Só frio"))
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "recommended_equipment" not in repository.context

    await engine.process(
        inbound(410, body="Unidade interna sem restrição de espaço")
    )
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "recommended_equipment" not in repository.context

    await engine.process(
        inbound(411, body="Unidade externa sem restrição de espaço")
    )
    assert repository.state == ConversationState.BOOKING_INSTALLATION_HEIGHT
    recommendation = repository.context["recommended_equipment"]
    assert recommendation["capacity_btu"] >= 9000
    transition = repository.outbounds[-1].transition
    combined = " ".join(
        message.body or ""
        for message in (transition.outbound, *transition.follow_ups)
    )
    assert recommendation["label"] in combined


@mark.asyncio
async def test_building_information_is_collected_before_availability() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_PROPERTY,
        context={
            "service_id": str(SERVICE_ID),
            "equipment_ownership": "has_equipment",
            "equipment_model_known": True,
            "equipment_model": "LG 12000 BTU",
            "installation_height_over_3m": False,
            "work_at_height": False,
            "tube_disclaimer_sent": True,
            "tubing_length_answered": True,
            "service_address": {
                "address_line": "Rua A, 10",
                "city": "São José dos Campos",
                "state": "SP",
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(409, action="property.building", body="Prédio")
    )
    assert repository.state == ConversationState.BOOKING_BUILDING_HOURS

    await engine.process(inbound(410, body="Das 08:00 às 17:00"))
    assert repository.state == ConversationState.BOOKING_GATE_DETAILS

    await engine.process(
        inbound(411, body="Bloco B, apartamento 42, falar com Alan na portaria")
    )
    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context["building_hours_start"] == "08:00"
    assert repository.context["building_hours_end"] == "17:00"
    assert "Bloco B" in repository.context["gate_instructions"]


@mark.asyncio
async def test_selected_time_collects_attendee_and_confirms_whatsapp_before_final_confirmation() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "property_type": "house",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(412, body="09:00", whatsapp_id="5512981359722")
    )
    assert repository.state == ConversationState.BOOKING_ATTENDEE

    await engine.process(
        inbound(
            413,
            action="attendee.other",
            body="Outra pessoa",
            whatsapp_id="5512981359722",
        )
    )
    assert repository.state == ConversationState.BOOKING_ATTENDEE_NAME

    await engine.process(
        inbound(414, body="Marcos", whatsapp_id="5512981359722")
    )
    assert repository.state == ConversationState.BOOKING_PHONE_CONFIRM
    assert "+5512981359722" in (
        repository.outbounds[-1].transition.outbound.body or ""
    )

    await engine.process(
        inbound(
            415,
            action="phone.confirm",
            body="Sim",
            whatsapp_id="5512981359722",
        )
    )
    assert repository.state == ConversationState.BOOKING_CONFIRM
    transition = repository.outbounds[-1].transition
    confirmation_body = " ".join(
        message.body or ""
        for message in (transition.outbound, *transition.follow_ups)
    )
    assert "Pessoa no local: Marcos" in confirmation_body
    assert "Contato: +5512981359722" in confirmation_body


@mark.asyncio
async def test_work_at_height_and_contact_are_persisted_in_booking_requirements() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "property_type": "house",
            "work_at_height": True,
            "installation_height_over_3m": True,
            "onsite_contact_mode": "customer",
            "onsite_contact_name": "Alan",
            "contact_phone": "+5512981359722",
            "contact_phone_confirmed": True,
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(416, action="booking.confirm", body="Confirmar")
    )

    assert repository.state == ConversationState.POST_BOOKING_HELP
    requirements = booking_port.confirmation_requirements[-1]
    assert requirements.operational_details["work_at_height"] is True
    assert requirements.operational_details["onsite_contact_name"] == "Alan"
    assert requirements.operational_details["contact_phone"] == "+5512981359722"


@mark.asyncio
async def test_quote_can_finish_without_creating_appointment() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.QUOTE_DECISION,
        context={
            "service_id": str(SERVICE_ID),
            "quote_presented": True,
            "request_mode": "quote",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(417, action="quote.finish", body="Só queria a cotação")
    )

    assert repository.state == ConversationState.COMPLETED
    assert "confirm" not in booking_port.calls

@mark.asyncio
async def test_equipment_quote_without_buy_verb_opens_purchase_clarification() -> None:
    repository = FakeConversationRepository(customer_name="Alan")
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(901, body="Gostaria de fazer a cotação de um ar condicionado")
    )

    transition = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_SERVICE
    assert transition.outbound.message_type == "interactive_button"
    body = (transition.outbound.body or "").casefold()
    assert "comprar" in body
    assert "instalação" in body
    assert "direcionar corretamente" not in body


@mark.asyncio
async def test_recommendation_flow_announces_quick_questions_before_asking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_MODEL,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(902, action="equipment.model.recommend", body="Não")
    )

    transition = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "perguntas rápidas" in (transition.outbound.body or "").casefold()
    assert transition.follow_ups
    assert "quantas pessoas" in (transition.follow_ups[0].body or "").casefold()
    assert repository.context["equipment_profile_intro_sent"] is True


@mark.asyncio
async def test_profile_accepts_bare_and_spelled_numbers_in_sequence() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(903, body="4"))

    assert repository.context["room_people_max"] == 4
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "tamanho" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(inbound(904, body="dezesseis"))

    assert repository.context["room_area_m2"] == 16
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert repository.outbounds[-1].transition.outbound.interactive_id == (
        "booking.equipment_preference"
    )


@mark.asyncio
async def test_profile_cycle_and_no_space_limit_buttons_are_not_treated_as_stale() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
            "room_people_max": 3,
            "room_area_m2": 16,
            "equipment_preference": "modern",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(905, action="equipment.cycle.heat_cool", body="Quente/frio")
    )

    assert repository.context["equipment_cycle"] == "heat_cool"
    first = repository.outbounds[-1].transition.outbound
    assert first.interactive_id == "booking.equipment_space.both"
    assert "interna" in (first.body or "").casefold()
    assert "externa" in (first.body or "").casefold()

    await engine.process(
        inbound(906, action="equipment.space.no_limit", body="Sem restrição")
    )

    assert repository.context["indoor_space_unrestricted"] is True
    assert repository.context["outdoor_space_unrestricted"] is True
    assert repository.state == ConversationState.BOOKING_INSTALLATION_HEIGHT


@mark.asyncio
async def test_profile_rephrases_once_then_handoffs_after_second_invalid_answer() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
            "room_people_max": 3,
            "room_area_m2": 16,
            "equipment_preference": "modern",
            "equipment_cycle": "cold",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(907, body="1000cm"))

    first = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert repository.context["repair_attempts"]["equipment_profile:indoor_space"] == 1
    retry_body = (first.outbound.body or "").casefold()
    assert "apertado" in retry_body
    assert "há limitação de espaço para a unidade interna" not in retry_body

    await engine.process(inbound(908, body="não faço ideia"))

    second = repository.outbounds[-1].transition
    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    handoff = (second.outbound.body or "").casefold()
    assert "vou chamar" in handoff
    assert "pessoa da nossa equipe" in handoff
    assert "aguarde" in handoff


@mark.asyncio
async def test_profile_accepts_typed_no_limit_for_both_spaces() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
            "room_people_max": 3,
            "room_area_m2": 16,
            "equipment_preference": "modern",
            "equipment_cycle": "cold",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(909, body="Não tem limitação")
    )

    assert repository.context["indoor_space_unrestricted"] is True
    assert repository.context["outdoor_space_unrestricted"] is True
    assert repository.state == ConversationState.BOOKING_INSTALLATION_HEIGHT

@mark.asyncio
async def test_building_hours_accepts_common_hrs_variants() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_BUILDING_HOURS,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "property_type": "condominium",
            "quote_presented": True,
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(920, body="Das 8hrs as 16hrs")
    )

    assert repository.context["building_hours_start"] == "08:00"
    assert repository.context["building_hours_end"] == "16:00"
    assert repository.context["site_allowed_end"] == "16:00"
    assert repository.context["site_limit_answered"] is True
    assert repository.state == ConversationState.BOOKING_GATE_DETAILS


@mark.asyncio
async def test_building_hours_rephrases_once_before_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_BUILDING_HOURS,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "property_type": "condominium",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(921, body="durante o dia"))

    first = repository.outbounds[-1].transition
    assert repository.state == ConversationState.BOOKING_BUILDING_HOURS
    assert repository.context["repair_attempts"]["building_hours"] == 1
    assert "apenas o intervalo" in (first.outbound.body or "").casefold()
    assert "qual é o horário permitido" not in (first.outbound.body or "").casefold()

    await engine.process(inbound(922, body="não sei dizer"))

    second = repository.outbounds[-1].transition
    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    body = (second.outbound.body or "").casefold()
    assert "vou chamar" in body
    assert "pessoa da nossa equipe" in body
    assert "aguarde" in body

@mark.asyncio
async def test_equipment_purchase_answer_does_not_restart_active_quote_or_repeat_address() -> None:
    original_address = {
        "address_line": "Rua Mauricio Cardoso, 201, Jardim Sul",
        "city": "São José dos Campos",
        "state": "SP",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "purchase_only": False,
            "service_address": original_address,
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(930, body="Quero cotar aparelho")
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["equipment_ownership"] == "needs_equipment"
    assert repository.context["service_address"] == original_address
    outbound = repository.outbounds[-1].transition.outbound
    assert outbound.interactive_id == "booking.equipment_model_known"
    body = (outbound.body or "").casefold()
    assert "comprar o aparelho" not in body
    assert "endereço completo" not in body


@mark.asyncio
async def test_space_question_is_short_and_explains_tight_internal_external_space() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
            "room_people_max": 4,
            "room_area_m2": 16,
            "equipment_preference": "economy",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(931, action="equipment.cycle.cold", body="Só frio")
    )

    outbound = repository.outbounds[-1].transition.outbound
    body = (outbound.body or "").casefold()
    assert outbound.interactive_id == "booking.equipment_space.both"
    assert "apertado" in body
    assert "interna" in body
    assert "externa" in body
    assert "sem restrição" in body


@mark.asyncio
async def test_recommendation_message_omits_thermal_load_credibility_caveat() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "equipment_profile_intro_sent": True,
            "room_people_max": 4,
            "room_area_m2": 16,
            "equipment_preference": "economy",
            "equipment_cycle": "cold",
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(932, action="equipment.space.no_limit", body="Sem restrição")
    )

    transition = repository.outbounds[-1].transition
    body = " ".join(
        message.body or ""
        for message in (transition.outbound, *transition.follow_ups)
    ).casefold()
    assert "carga térmica fora do padrão" not in body
    assert "boa referência" in body
    assert repository.context["recommended_equipment"]["selected_cycle"] == "cold"
    assert "só frio" in repository.context["recommended_equipment"]["label"].casefold()


@mark.asyncio
async def test_quote_price_does_not_repeat_tubing_disclaimer_and_invites_scheduling() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_PROPERTY,
        context={
            "service_id": str(SERVICE_ID),
            "request_mode": "quote",
            "purchase_only": False,
            "equipment_ownership": "needs_equipment",
            "equipment_model_known": False,
            "recommended_equipment": {
                "item_id": "catalog-gree-9000",
                "label": "Gree G-Top Auto Inverter 9.000 BTU — Só Frio",
                "capacity_btu": 9000,
                "selected_cycle": "cold",
            },
            "installation_height_over_3m": False,
            "work_at_height": False,
            "tube_disclaimer_sent": True,
            "tubing_length_answered": True,
            "service_address": {
                "address_line": "Rua A, 10",
                "city": "São José dos Campos",
                "state": "SP",
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
        asks_tubing_length=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(933, action="property.house", body="Casa")
    )

    assert repository.state == ConversationState.QUOTE_DECISION
    transition = repository.outbounds[-1].transition
    messages = (transition.outbound, *transition.follow_ups)
    joined = " ".join(message.body or "" for message in messages).casefold()
    assert "instalação/serviço" in joined
    assert "metragem incluída de tubulação" not in joined
    assert "material adicional pode alterar o valor" not in joined
    decision = next(
        message for message in messages
        if message.interactive_id == "quote.decision"
    )
    assert decision.body == "Gostaria de já agendar a instalação?"


@mark.asyncio
async def test_final_confirmation_is_bulleted_and_completion_sends_farewell_then_reacts() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "property_type": "house",
            "service_address": {
                "address_line": "Rua A, 10",
                "city": "São José dos Campos",
                "state": "SP",
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(934, body="09:00", whatsapp_id="5512981359722")
    )
    await engine.process(
        inbound(
            935,
            action="attendee.customer",
            body="Eu mesmo",
            whatsapp_id="5512981359722",
        )
    )
    await engine.process(
        inbound(
            936,
            action="phone.confirm",
            body="Sim",
            whatsapp_id="5512981359722",
        )
    )

    assert repository.state == ConversationState.BOOKING_CONFIRM
    confirmation = repository.outbounds[-1].transition.outbound.body or ""
    assert "• Serviço:" in confirmation
    assert "• Data:" in confirmation
    assert "• Horário:" in confirmation
    assert "• Endereço:" in confirmation
    assert "\n" in confirmation

    await engine.process(
        inbound(937, action="booking.confirm", body="Confirmar")
    )

    completed = repository.outbounds[-1].transition
    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert "Agendamento confirmado" in (completed.outbound.body or "")
    assert completed.follow_ups
    help_message = completed.follow_ups[0]
    assert help_message.interactive_id == "post_booking.help"
    assert "mais algum assunto" in (help_message.body or "").casefold()

    await engine.process(
        inbound(938, action="post_booking.help.no", body="Não, obrigado")
    )
    farewell = repository.outbounds[-1].transition.outbound.body or ""
    assert repository.state == ConversationState.COMPLETED
    assert "Muito obrigado pela preferência" in farewell
    assert "Até logo" in farewell

    await engine.process(inbound(939, body="Obrigado"))

    reaction = repository.outbounds[-1].transition.outbound
    assert repository.state == ConversationState.COMPLETED
    assert reaction.message_type == "reaction"
    assert reaction.body is None
    assert reaction.outbound_payload == {
        "message_id": "provider-939",
        "emoji": "👍",
    }

@mark.asyncio
async def test_missing_city_is_asked_directly_without_business_city_guess() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    booking_port.business_city = "São José dos Campos"
    booking_port.business_state = "SP"
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(940, body="Avenida Charles Schneider, 1700, Vila Costa")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    outbound = repository.outbounds[-1].transition.outbound
    assert outbound.message_type == "text"
    assert outbound.interactive_id is None
    body = (outbound.body or "").casefold()
    assert "apenas a cidade" in body
    assert "são josé dos campos" not in body
    assert repository.context["awaiting_address_city"] is True

    await engine.process(inbound(941, body="Taubaté"))

    assert repository.context["service_address"]["city"] == "Taubaté"
    assert repository.state == ConversationState.BOOKING_DATE


@mark.asyncio
async def test_common_city_abbreviation_bh_is_normalized() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "pending_service_address": "Rua A, 20, Centro",
            "awaiting_address_city": True,
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(942, body="BH")
    )

    address = repository.context["service_address"]
    assert address["city"] == "Belo Horizonte"
    assert address["state"] == "MG"
    assert repository.state == ConversationState.BOOKING_DATE


@mark.asyncio
async def test_noise_diagnostic_collects_model_photo_and_video_before_agenda() -> None:
    repository = FakeConversationRepository(customer_name="Alan")
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Diagnóstico / manutenção corretiva")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(943, body="Meu aparelho de ar condicionado está fazendo barulho")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["issue_video_required"] is True
    assert "barulho" in repository.context["reported_issue"].casefold()

    await engine.process(
        inbound(944, body="Rua A, 10, Centro, SJC")
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert "marca e o modelo" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(inbound(945, body="Não sei"))

    assert repository.context["equipment_photo_requested"] is True
    assert "foto" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(
        inbound(946, message_type="image")
    )

    assert repository.context["equipment_photo_received"] is True
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert "vídeo" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(
        inbound(947, message_type="video")
    )

    assert repository.context["issue_video_received"] is True
    assert repository.state == ConversationState.BOOKING_DATE
    assert "vídeo" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@mark.asyncio
async def test_gas_recharge_collects_existing_equipment_before_agenda() -> None:
    repository = FakeConversationRepository(customer_name="Alan")
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Recarga de gás e teste de vazamento")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(948, body="Gostaria de fazer a reposição do gás do meu ar condicionado")
    )
    assert repository.state == ConversationState.BOOKING_ADDRESS

    await engine.process(
        inbound(949, body="Rua A, 10, Centro, SJC")
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.context["equipment_ownership"] == "has_equipment"


@mark.asyncio
async def test_post_booking_supported_request_starts_new_supported_flow() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context={},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização")
    ]
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=True,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(950, body="Também gostaria de fazer uma limpeza")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True


@mark.asyncio
async def test_post_booking_unrelated_subject_goes_to_human_team() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.POST_BOOKING_HELP,
        context={},
        customer_name="Alan",
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(951, body="Também queria falar sobre um assunto financeiro diferente")
    )

    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False


@mark.parametrize(
    ("state", "context"),
    [
        (
            ConversationState.BOOKING_ADDRESS,
            {"service_id": str(SERVICE_ID)},
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
        ),
    ],
)
@mark.asyncio
async def test_lateral_question_resumes_active_slot_without_repair_attempt(
    state: ConversationState,
    context: dict[str, Any],
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    booking_port.intake = replace(
        booking_port.intake,
        requires_address=state is ConversationState.BOOKING_ADDRESS,
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(960, body="Quanto custa esse serviço?")
    )

    assert repository.state == state
    assert "repair_attempts" not in repository.context
    assert "R$ 100,00" in (
        repository.outbounds[-1].transition.outbound.body or ""
    )


@mark.asyncio
async def test_two_lateral_questions_and_social_reply_never_force_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "repair_attempts": {"address": 1},
        },
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(961, body="Quanto custa?"))
    await engine.process(inbound(962, body="Quanto tempo demora?"))
    await engine.process(inbound(963, body="Obrigado"))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True
    assert repository.context["repair_attempts"] == {"address": 1}


@mark.asyncio
async def test_old_button_requires_confirmation_and_keep_preserves_current_slot() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(
        inbound(964, action="property.house", body="Casa")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["pending_change_action"] == "property.house"
    assert "quer mudar" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(
        inbound(965, action="change.keep", body="Não, continuar")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert "pending_change_action" not in repository.context
    assert "property_type" not in repository.context


@mark.asyncio
async def test_old_button_confirm_applies_change_without_clearing_service() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(
        inbound(966, action="property.house", body="Casa")
    )
    await engine.process(
        inbound(967, action="change.confirm", body="Sim, mudar")
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["property_type"] == "house"
    assert "pending_change_action" not in repository.context
    assert repository.automation_enabled is True


@mark.asyncio
async def test_additional_service_request_does_not_replace_active_request() -> None:
    maintenance_id = uuid.UUID("41000000-0000-0000-0000-000000000004")
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(str(SERVICE_ID), "Limpeza e higienização"),
        BookingOption(
            str(maintenance_id),
            "Diagnóstico e manutenção corretiva",
        ),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(968, body="Também quero manutenção")
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["pending_service_change_id"] == str(
        maintenance_id
    )
    assert repository.state == ConversationState.BOOKING_DATE
    assert "concluir este atendimento" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


async def _complete_equipment_profile(
    engine: ConversationEngine,
    *,
    sequence: int,
) -> None:
    steps = (
        ("equipment.model.recommend", "Não tenho modelo"),
        (None, "3 pessoas"),
        (None, "16 m2"),
        ("equipment.preference.cost_benefit", "Custo-benefício"),
        ("equipment.cycle.cold", "Só frio"),
        ("equipment.space.no_limit", "Sem restrição"),
    )
    for offset, (action, body) in enumerate(steps):
        await engine.process(
            inbound(sequence + offset, action=action, body=body)
        )


@mark.asyncio
async def test_purchase_only_recommends_once_and_pickup_finishes_without_handoff() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={"service_clarification": "equipment_purchase"},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(970, action="equipment.purchase", body="Só comprar")
    )
    await _complete_equipment_profile(engine, sequence=971)

    recommendation = repository.outbounds[-1].transition
    messages = (recommendation.outbound, *recommendation.follow_ups)
    combined = " ".join(message.body or "" for message in messages)
    assert combined.casefold().count("uma boa referência") == 1
    assert "9.000 BTU" in combined
    assert "Só Frio" in combined
    assert "R$ 2.500,00" in combined
    images = [message for message in messages if message.message_type == "image"]
    assert len(images) == 1
    assert images[0].sequence_optional is True
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_DELIVERY

    await engine.process(
        inbound(978, action="equipment.delivery.pickup", body="Retirar")
    )

    assert repository.state == ConversationState.COMPLETED
    assert repository.automation_enabled is True
    assert repository.context["purchase_mode"] == "purchase"
    assert "service_address" not in repository.context


@mark.asyncio
async def test_purchase_and_install_keeps_delivery_and_service_addresses_separate() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={"service_clarification": "equipment_purchase"},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(980, action="equipment.both", body="Comprar e instalar")
    )
    await _complete_equipment_profile(engine, sequence=981)
    await engine.process(
        inbound(987, action="equipment.delivery.address", body="Receber")
    )
    await engine.process(
        inbound(
            988,
            body="Rua A, 10, Centro, São José dos Campos - SP",
        )
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_DELIVERY
    assert "delivery_address" in repository.context
    assert "service_address" not in repository.context

    await engine.process(
        inbound(
            989,
            action="equipment.installation.same_address",
            body="Mesmo endereço",
        )
    )

    assert repository.context["purchase_mode"] == "both"
    assert repository.context["service_address"] == repository.context[
        "delivery_address"
    ]
    assert repository.automation_enabled is True


@mark.asyncio
async def test_requested_equipment_photo_is_recorded_and_flow_continues() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_MODEL,
        context={
            "service_id": str(SERVICE_ID),
            "equipment_ownership": "has_equipment",
            "equipment_photo_requested": True,
        },
    )

    await ConversationEngine(repository, FakeBookingPort()).process(
        inbound(990, message_type="image")
    )

    assert repository.context["equipment_photo_received"] is True
    assert repository.automation_enabled is True


@mark.asyncio
async def test_duplicate_hardening_event_keeps_outbox_idempotent() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)
    event = inbound(991, body="Obrigado")

    assert await engine.process(event) is True
    assert await engine.process(event) is False
    assert len(repository.outbounds) == 1



@mark.asyncio
async def test_compound_greeting_during_slot_does_not_consume_retry() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "repair_attempts": {"address": 1},
        },
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(booking_port.intake, requires_address=True)

    await ConversationEngine(repository, booking_port).process(
        inbound(980, body="Bom dia, tudo bem?")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["repair_attempts"] == {"address": 1}
    assert repository.automation_enabled is True


@mark.asyncio
async def test_old_quantity_button_confirms_change_and_invalidates_schedule() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "quantity": 1,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
        },
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(
        inbound(981, action="quantity:2", body="2")
    )
    assert repository.context["pending_change_action"] == "quantity:2"
    assert "quer mudar" in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()

    await engine.process(
        inbound(982, action="change.confirm", body="Sim, mudar")
    )

    assert repository.context["quantity"] == 2
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "pending_change_action" not in repository.context
    assert repository.automation_enabled is True


@mark.asyncio
async def test_unexpected_audio_never_self_deprecates_and_continue_text_resumes_slot() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={
            "service_id": str(SERVICE_ID),
            "repair_attempts": {"address": 1},
        },
    )
    booking_port = FakeBookingPort()
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(inbound(1100, message_type="audio"))

    media_prompt = repository.outbounds[-1].transition.outbound
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["media_handoff_pending"] is True
    assert repository.context["repair_attempts"] == {"address": 1}
    assert "não consigo analisar" not in (media_prompt.body or "").casefold()
    assert "encaminhei para a equipe" in (media_prompt.body or "").casefold()
    assert media_prompt.interactive_id == "media.unsupported"

    await engine.process(
        inbound(
            1101,
            action="media.continue_text",
            body="Continuar por texto",
        )
    )

    resumed = repository.outbounds[-1].transition.outbound.body or ""
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "media_handoff_pending" not in repository.context
    assert repository.context["repair_attempts"] == {"address": 1}
    assert "seguimos por texto" in resumed.casefold()
    assert "endereço" in resumed.casefold()


@mark.asyncio
async def test_purchase_and_install_offers_delivery_with_technician() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={"service_clarification": "equipment_purchase"},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(1110, action="equipment.both", body="Comprar e instalar")
    )
    await _complete_equipment_profile(engine, sequence=1111)

    transition = repository.outbounds[-1].transition
    messages = (transition.outbound, *transition.follow_ups)
    delivery = next(
        message
        for message in messages
        if message.interactive_id == "equipment.delivery"
    )
    button_ids = {
        button["id"]
        for button in (delivery.outbound_payload or {}).get("buttons", [])
    }
    assert "equipment.delivery.pickup" in button_ids
    assert "equipment.delivery.address" in button_ids
    assert "equipment.delivery.with_installation" in button_ids

    await engine.process(
        inbound(
            1120,
            action="equipment.delivery.with_installation",
            body="Levar com a instalação",
        )
    )

    assert repository.context["delivery_method"] == "with_installation"
    assert "delivery_address" not in repository.context
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context.get("address_purpose") != "delivery"


@mark.asyncio
async def test_purchase_only_does_not_offer_delivery_with_technician() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        context={"service_clarification": "equipment_purchase"},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)
    engine = ConversationEngine(repository, booking_port)

    await engine.process(
        inbound(1130, action="equipment.purchase", body="Só comprar")
    )
    await _complete_equipment_profile(engine, sequence=1131)

    transition = repository.outbounds[-1].transition
    messages = (transition.outbound, *transition.follow_ups)
    delivery = next(
        message
        for message in messages
        if message.interactive_id == "equipment.delivery"
    )
    button_ids = {
        button["id"]
        for button in (delivery.outbound_payload or {}).get("buttons", [])
    }
    assert "equipment.delivery.with_installation" not in button_ids


@mark.asyncio
async def test_diagnostics_price_is_presented_as_base_value_and_resumes_pending_slot() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_ADDRESS,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Diagnóstico / manutenção corretiva",
        )
    ]
    booking_port.intake = replace(booking_port.intake, requires_address=True)

    await ConversationEngine(repository, booking_port).process(
        inbound(1140, body="Quanto custa a manutenção?")
    )

    body = repository.outbounds[-1].transition.outbound.body or ""
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "valor base do atendimento técnico" in body.casefold()
    assert "valor final" in body.casefold()
    assert "diagnóstico" in body.casefold()
    assert "endereço" in body.casefold()


@mark.asyncio
async def test_completed_equipment_feature_question_is_answered_without_inventing_alexa() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        context={
            "equipment_cycle": "cold",
            "recommended_equipment": {
                "price": 2500.0,
                "required_btu_reference": 9000,
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(
            1150,
            body=(
                "Tem algum modelo mais em conta que eu ainda consiga "
                "conectar com Alexa?"
            ),
        )
    )

    transition = repository.outbounds[-1].transition
    body = transition.outbound.body or ""
    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert "alexa" in body.casefold()
    assert (
        "explicitamente" in body.casefold()
        or "não tenho compatibilidade" in body.casefold()
    )
    assert "posso ajudar com limpeza" not in body.casefold()
    assert transition.follow_ups
    assert transition.follow_ups[0].interactive_id == "post_booking.help"


@mark.asyncio
async def test_completed_budget_question_returns_catalog_option_and_post_help() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        context={
            "equipment_cycle": "cold",
            "recommended_equipment": {
                "price": 3000.0,
                "required_btu_reference": 9000,
            },
        },
        customer_name="Alan",
    )
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(
            1160,
            body="Quero um ar-condicionado com Wi-Fi até R$ 2.600",
        )
    )

    transition = repository.outbounds[-1].transition
    body = transition.outbound.body or ""
    assert repository.state == ConversationState.POST_BOOKING_HELP
    assert "Gree" in body
    assert "R$ 2.500,00" in body
    assert transition.follow_ups
    assert transition.follow_ups[0].interactive_id == "post_booking.help"


@mark.asyncio
async def test_purchase_and_install_confirmation_separates_service_equipment_and_total() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "purchase_mode": "both",
            "recommended_equipment": {
                "label": "Gree G-Top Auto Inverter 9.000 BTU — Só Frio",
                "price": 2500.0,
            },
        },
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Instalação de ar-condicionado split",
        )
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1170, body="quero revisar")
    )

    body = repository.outbounds[-1].transition.outbound.body or ""
    assert "Valor do serviço: R$ 100,00" in body
    assert "Valor do equipamento: R$ 2.500,00" in body
    assert "Total: R$ 2.600,00" in body


@mark.asyncio
async def test_diagnostics_confirmation_marks_service_amount_as_base() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.services = [
        BookingOption(
            str(SERVICE_ID),
            "Diagnóstico / manutenção corretiva",
        )
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(1180, body="quero revisar")
    )

    body = repository.outbounds[-1].transition.outbound.body or ""
    assert "Valor base do serviço técnico: R$ 100,00" in body
    assert "valor final" in body.casefold()
    assert "peças" in body.casefold()

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.booking.domain import (
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceEstimate,
    ServiceIntake,
    TravelEstimate,
)
from app.booking.equipment_recommender import EquipmentCatalogEntry
from app.conversations.constants import ConversationState
from app.conversations.ports import (
    BookingConfirmation,
    BookingOption,
    ExistingBooking,
    ServiceDetails,
)
from app.conversations.types import ConversationInput, ConversationSnapshot, ConversationTransition

BUSINESS_ID = uuid.UUID("91000000-0000-0000-0000-000000000001")
CUSTOMER_ID = uuid.UUID("92000000-0000-0000-0000-000000000002")
CONVERSATION_ID = uuid.UUID("93000000-0000-0000-0000-000000000003")
SERVICE_ID = uuid.UUID("94000000-0000-0000-0000-000000000004")
INSTALLATION_SERVICE_ID = uuid.UUID("94000000-0000-0000-0000-000000000007")
DIAGNOSTICS_SERVICE_ID = uuid.UUID("94000000-0000-0000-0000-000000000008")
GAS_SERVICE_ID = uuid.UUID("94000000-0000-0000-0000-000000000009")
APPOINTMENT_ID = uuid.UUID("95000000-0000-0000-0000-000000000005")
EMPLOYEE_ID = uuid.UUID("96000000-0000-0000-0000-000000000006")


@dataclass(frozen=True, slots=True)
class StoredOutbound:
    transition: ConversationTransition
    idempotency_key: str


class LabConversationRepository:
    """Repositório 100% em memória. Nunca toca banco, Meta ou dados reais."""

    def __init__(
        self,
        *,
        state: str = ConversationState.START,
        context: dict[str, Any] | None = None,
        customer_name: str | None = "Cliente Sintético",
    ) -> None:
        self.state = state
        self.context = context or {}
        self.customer_name = customer_name
        self.automation_enabled = True
        self.handoff_status = "none"
        self.assistant_enabled = True
        self.outbounds: list[StoredOutbound] = []
        self.idempotency_keys: set[str] = set()
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lock_conversation(self, business_id: uuid.UUID, conversation_id: uuid.UUID) -> AsyncIterator[ConversationSnapshot]:
        if business_id != BUSINESS_ID or conversation_id != CONVERSATION_ID:
            raise RuntimeError("conversation_lab_tenant_guard")
        async with self.lock:
            yield ConversationSnapshot(
                business_id=BUSINESS_ID,
                customer_id=CUSTOMER_ID,
                conversation_id=CONVERSATION_ID,
                state=self.state,
                context=copy.deepcopy(self.context),
                automation_enabled=self.automation_enabled,
                handoff_status=self.handoff_status,
                assistant_enabled=self.assistant_enabled,
                greeting_message="Olá! Como posso ajudar com seu ar-condicionado?",
                fallback_message="Desculpa, não entendi. Como posso ajudar?",
                handoff_message="Seu atendimento foi encaminhado para uma pessoa da equipe.",
                customer_name=self.customer_name,
                business_timezone="America/Sao_Paulo",
                business_name="Empresa Sintética Alovia",
            )

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
        self.state = transition.state.value
        self.context = copy.deepcopy(transition.context)
        self.automation_enabled = transition.automation_enabled
        self.handoff_status = transition.handoff_status
        if transition.customer_name is not None:
            self.customer_name = transition.customer_name
        return True


class DryRunBookingPort:
    """Adapter sintético. Toda ação destrutiva é registrada, nunca executada externamente."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.services = [
            BookingOption(str(SERVICE_ID), "Limpeza e higienização"),
            BookingOption(
                str(INSTALLATION_SERVICE_ID),
                "Instalação de ar-condicionado split",
            ),
            BookingOption(
                str(DIAGNOSTICS_SERVICE_ID),
                "Diagnóstico / manutenção corretiva",
            ),
            BookingOption(
                str(GAS_SERVICE_ID),
                "Recarga de gás e teste de vazamento",
            ),
        ]
        self.dates = [
            BookingOption("2026-11-16", "segunda-feira, 16 de novembro"),
            BookingOption("2026-11-18", "quarta-feira, 18 de novembro"),
        ]
        self.times = [
            BookingOption("09:00", "09:00"),
            BookingOption("14:30", "14:30"),
            BookingOption("18:00", "18:00"),
        ]
        self.intake = ServiceIntake(
            requires_quantity=True,
            requires_address=True,
            considers_difficult_access=False,
            asks_site_time_limit=False,
            automatic_booking=True,
            pricing_type=PricingType.ESTIMATED,
        )
        self.service_intakes = {
            SERVICE_ID: self.intake,
            INSTALLATION_SERVICE_ID: ServiceIntake(
                requires_quantity=True,
                requires_address=True,
                considers_difficult_access=False,
                asks_site_time_limit=False,
                automatic_booking=True,
                pricing_type=PricingType.ESTIMATED,
                asks_tubing_length=True,
            ),
            DIAGNOSTICS_SERVICE_ID: ServiceIntake(
                requires_quantity=False,
                requires_address=True,
                considers_difficult_access=False,
                asks_site_time_limit=False,
                automatic_booking=True,
                pricing_type=PricingType.ESTIMATED,
            ),
            GAS_SERVICE_ID: ServiceIntake(
                requires_quantity=False,
                requires_address=True,
                considers_difficult_access=False,
                asks_site_time_limit=False,
                automatic_booking=True,
                pricing_type=PricingType.ESTIMATED,
            ),
        }
        self.plan = BookingPlan(
            service=ServiceEstimate(
                estimated_duration_minutes=30,
                estimated_price=Decimal("100.00"),
                pricing_type=PricingType.ESTIMATED,
                requires_human_quote=False,
                applied_rules=("conversation_lab",),
                qualifier="estimated",
            ),
            travel=TravelEstimate(
                travel_minutes=0,
                distance_km=None,
                source="conversation_lab",
                method="synthetic",
                estimated=False,
            ),
            travel_before_minutes=0,
            travel_after_minutes=0,
            requires_handoff=False,
        )
        starts_at = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.confirmation = BookingConfirmation(
            appointment_id=APPOINTMENT_ID,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            employee_id=EMPLOYEE_ID,
        )

    async def list_services(self, _: uuid.UUID):
        self.calls.append("services")
        return tuple(self.services)

    async def get_service_details(self, _: uuid.UUID, service_id: uuid.UUID):
        service = next(
            (item for item in self.services if item.id == str(service_id)),
            BookingOption(str(service_id), "Serviço sintético"),
        )
        has_tubing_details = service_id in {
            SERVICE_ID,
            INSTALLATION_SERVICE_ID,
        }
        return ServiceDetails(
            id=service_id,
            name=service.label,
            description=f"{service.label} configurado no laboratório.",
            included_tubing_meters=Decimal("3") if has_tubing_details else None,
            extra_tubing_price=Decimal("50") if has_tubing_details else None,
            extra_tubing_unit_label="metro" if has_tubing_details else None,
            business_city="São José dos Campos",
            business_state="SP",
            business_address=None,
        )

    async def get_service_intake(self, _: uuid.UUID, service_id: uuid.UUID):
        return self.service_intakes.get(service_id, self.intake)

    async def list_equipment_catalog(self, _: uuid.UUID):
        return tuple[EquipmentCatalogEntry]()

    async def estimate(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
        requirements: BookingRequirements,
    ):
        self.calls.append("estimate")

        if service_id == SERVICE_ID:
            quantity = requirements.quantity or 1
            additional_units = max(quantity - 1, 0)
            additional_price = Decimal("80") * additional_units
            applied_rules = ["conversation_lab"]
            if additional_price:
                applied_rules.append(f"additional_units_price:{additional_price}")
            return BookingPlan(
                service=ServiceEstimate(
                    estimated_duration_minutes=30 + (20 * additional_units),
                    estimated_price=Decimal("100") + additional_price,
                    pricing_type=PricingType.ESTIMATED,
                    requires_human_quote=False,
                    applied_rules=tuple(applied_rules),
                    qualifier="estimated",
                ),
                travel=self.plan.travel,
                travel_before_minutes=0,
                travel_after_minutes=0,
                requires_handoff=False,
            )

        if service_id == INSTALLATION_SERVICE_ID:
            tubing = requirements.tubing_meters or Decimal("3")
            extra_meters = max(tubing - Decimal("3"), Decimal("0"))
            extra_price = extra_meters * Decimal("50")
            applied_rules = ["conversation_lab"]
            if extra_price:
                applied_rules.append(f"extra_tubing_price:{extra_price}")
            return BookingPlan(
                service=ServiceEstimate(
                    estimated_duration_minutes=120 + int(extra_meters * 10),
                    estimated_price=Decimal("700") + extra_price,
                    pricing_type=PricingType.ESTIMATED,
                    requires_human_quote=False,
                    applied_rules=tuple(applied_rules),
                    qualifier="estimated",
                ),
                travel=self.plan.travel,
                travel_before_minutes=0,
                travel_after_minutes=0,
                requires_handoff=False,
            )

        if service_id == DIAGNOSTICS_SERVICE_ID:
            return BookingPlan(
                service=ServiceEstimate(
                    estimated_duration_minutes=60,
                    estimated_price=Decimal("180"),
                    pricing_type=PricingType.ESTIMATED,
                    requires_human_quote=False,
                    applied_rules=("conversation_lab",),
                    qualifier="estimated",
                ),
                travel=self.plan.travel,
                travel_before_minutes=0,
                travel_after_minutes=0,
                requires_handoff=False,
            )

        if service_id == GAS_SERVICE_ID:
            return BookingPlan(
                service=ServiceEstimate(
                    estimated_duration_minutes=60,
                    estimated_price=Decimal("250"),
                    pricing_type=PricingType.ESTIMATED,
                    requires_human_quote=False,
                    applied_rules=("conversation_lab",),
                    qualifier="estimated",
                ),
                travel=self.plan.travel,
                travel_before_minutes=0,
                travel_after_minutes=0,
                requires_handoff=False,
            )

        return self.plan

    async def list_dates(self, _: uuid.UUID, __: uuid.UUID, ___: BookingRequirements = BookingRequirements()):
        self.calls.append("dates")
        return tuple(self.dates)

    async def list_times(self, _: uuid.UUID, __: uuid.UUID, selected_date: str, ___: BookingRequirements = BookingRequirements()):
        self.calls.append("times")
        return tuple(self.times)

    async def confirm(self, *args: Any, **kwargs: Any):
        self.calls.append("confirm_dry_run")
        return self.confirmation

    async def list_customer_bookings(self, _: uuid.UUID, __: uuid.UUID):
        self.calls.append("existing_bookings")
        return (
            ExistingBooking(
                appointment_id=APPOINTMENT_ID,
                service_id=SERVICE_ID,
                label="Agendamento sintético",
                requirements=BookingRequirements(),
            ),
        )

    async def cancel_booking(self, *_: Any, **__: Any):
        self.calls.append("cancel_dry_run")
        return self.confirmation

    async def reschedule_booking_atomic(self, *_: Any, **__: Any):
        self.calls.append("reschedule_dry_run")
        return self.confirmation


def inbound(sequence: int, *, body: str | None = None, action: str | None = None) -> ConversationInput:
    return ConversationInput(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        provider_message_id=f"conversation-lab-{sequence}",
        message_type="interactive" if action else "text",
        body=body,
        interactive_id=action,
        whatsapp_id="5511999999999",
    )

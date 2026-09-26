from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


import pytest
from fastapi import HTTPException


from app.models import Business, BusinessCatalogItem, Service
from app.operations.schemas import SetupStatus
from app.operations.catalog import (
    catalog_preset_definitions,
    ensure_business_catalog_presets,
)
from app.operations.service import OperationalService, _catalog_item_view


BUSINESS_ID = uuid.UUID("c0000000-0000-0000-0000-000000000001")
PRESET_KEYS = (
    "extra-tubing-meter",
    "extra-drain-meter",
    "extra-electrical-cable-meter",
    "condenser-bracket",
    "wall-bracket-fixings",
)


class ScalarRows:
    def __init__(self, values: list[Any] | tuple[Any, ...]) -> None:
        self.values = list(values)

    def all(self) -> list[Any]:
        return self.values


class SetupSession:
    def __init__(
        self,
        company: Business,
        *,
        technicians: int = 1,
        hours: int = 1,
        services: tuple[Service, ...] = (),
        materials: tuple[BusinessCatalogItem, ...] = (),
        connected_whatsapp: int = 0,
    ) -> None:
        self.scalar_values = [
            company,
            technicians,
            hours,
            connected_whatsapp,
        ]
        self.scalar_rows = [
            ScalarRows(PRESET_KEYS),
            ScalarRows(services),
            ScalarRows(materials),
        ]

    async def scalar(self, _: object) -> Any:
        return self.scalar_values.pop(0)

    async def scalars(self, _: object) -> ScalarRows:
        return self.scalar_rows.pop(0)

    def add_all(self, _: list[BusinessCatalogItem]) -> None:
        return None

    async def commit(self) -> None:
        return None


def company(**changes: Any) -> Business:
    item = Business(
        id=BUSINESS_ID,
        name="Empresa",
        responsible_name="Responsável",
        timezone="America/Sao_Paulo",
        service_origin_address="Rua Itumbiara, 160 - Parque Industrial, São José dos Campos - SP, CEP 12235-740",
        service_origin_postal_code="12235740",
        service_origin_street="Rua Itumbiara",
        service_origin_neighborhood="Parque Industrial",
        service_origin_number="160",
        service_origin_city="São José dos Campos",
        service_origin_state="SP",
        service_origin_validated_at=datetime(2026, 9, 23, tzinfo=UTC),
        materials_catalog_reviewed=True,
        agenda_preferences_reviewed=True,
        onboarding_version=0,
        assistant_enabled=True,
        active=True,
    )
    item.meta_phone_number_id = None
    item.onboarding_completed_at = None
    for key, value in changes.items():
        setattr(item, key, value)
    return item


def priced_service() -> Service:
    return Service(
        business_id=BUSINESS_ID,
        name="Limpeza",
        duration_minutes=90,
        base_price=Decimal("150.00"),
        pricing_type="estimated",
        automatic_booking=True,
        active=True,
    )


def material(*, price: Decimal | None) -> BusinessCatalogItem:
    return BusinessCatalogItem(
        business_id=BUSINESS_ID,
        kind="material",
        name="Material",
        price=price,
        active=True,
    )


@pytest.mark.asyncio
async def test_materials_opt_out_completes_step_without_active_items() -> None:
    session = SetupSession(
        company(), services=(priced_service(),), connected_whatsapp=1
    )

    status = await OperationalService(session).setup_status(BUSINESS_ID)  # type: ignore[arg-type]

    assert status.materials is True
    assert status.completed == 7
    assert status.next_step == "complete"


@pytest.mark.asyncio
async def test_active_material_without_price_keeps_step_incomplete() -> None:
    session = SetupSession(
        company(),
        services=(priced_service(),),
        materials=(material(price=None),),
        connected_whatsapp=1,
    )

    status = await OperationalService(session).setup_status(BUSINESS_ID)  # type: ignore[arg-type]

    assert status.materials is False
    assert status.next_step == "materials"


@pytest.mark.asyncio
async def test_legacy_pilot_phone_identifier_is_valid_whatsapp_evidence() -> None:
    session = SetupSession(
        company(meta_phone_number_id="legacy-pilot-phone-id"),
        services=(priced_service(),),
        connected_whatsapp=0,
    )

    status = await OperationalService(session).setup_status(BUSINESS_ID)  # type: ignore[arg-type]

    assert status.whatsapp is True
    assert status.completed == 7


@pytest.mark.asyncio
async def test_missing_connection_and_legacy_identifier_keeps_whatsapp_incomplete() -> None:
    session = SetupSession(
        company(meta_phone_number_id=None),
        services=(priced_service(),),
        connected_whatsapp=0,
    )

    status = await OperationalService(session).setup_status(BUSINESS_ID)  # type: ignore[arg-type]

    assert status.whatsapp is False
    assert status.next_step == "whatsapp"


@pytest.mark.asyncio
async def test_complete_onboarding_allows_whatsapp_as_only_pending_step() -> None:
    session = SimpleNamespace(commit=AsyncMock())
    service = OperationalService(session)  # type: ignore[arg-type]
    business = company()
    pending = SetupStatus(
        company=True,
        team=True,
        business_hours=True,
        services=True,
        materials=True,
        agenda=True,
        whatsapp=False,
        completed=6,
        next_step="whatsapp",
        blocking_reasons=["Conexão com o WhatsApp Business pendente."],
    )
    completed = pending.model_copy(update={"onboarding_completed": True})
    service.setup_status = AsyncMock(side_effect=[pending, completed])  # type: ignore[method-assign]
    service._business = AsyncMock(return_value=business)  # type: ignore[method-assign]

    result = await service.complete_onboarding(BUSINESS_ID)

    assert business.onboarding_completed_at is not None
    assert business.onboarding_version == 1
    assert result.onboarding_completed is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_onboarding_still_rejects_any_required_step_missing() -> None:
    session = SimpleNamespace(commit=AsyncMock())
    service = OperationalService(session)  # type: ignore[arg-type]
    incomplete = SetupStatus(
        company=True,
        team=True,
        business_hours=True,
        services=False,
        materials=True,
        agenda=True,
        whatsapp=False,
        completed=5,
        next_step="services",
        blocking_reasons=[
            "Mantenha pelo menos um serviço ativo com preço definido.",
            "Conexão com o WhatsApp Business pendente.",
        ],
    )
    service.setup_status = AsyncMock(return_value=incomplete)  # type: ignore[method-assign]
    service._business = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        await service.complete_onboarding(BUSINESS_ID)

    assert exc_info.value.status_code == 409
    service._business.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_paid_catalog_defines_thirty_equipment_presets() -> None:
    definitions = catalog_preset_definitions()
    equipment = [item for item in definitions if item[0].startswith("equipment:")]

    assert len(equipment) == 30
    assert len(definitions) == 35
    assert len({item[0] for item in definitions}) == len(definitions)


def test_equipment_catalog_contract_exposes_verified_reference_details() -> None:
    item = BusinessCatalogItem(
        id=uuid.uuid4(),
        business_id=BUSINESS_ID,
        preset_key="equipment:lg-ai-dual-inverter-voice-9000",
        kind="equipment",
        name="LG AI Dual Inverter Voice 9.000 BTU",
        active=True,
    )

    view = _catalog_item_view(item)

    assert view.equipment_details is not None
    assert view.equipment_details.brand == "LG"
    assert view.equipment_details.capacity_btu == 9000
    assert view.equipment_details.cycles == ["cooling_only", "heat_cool"]
    assert view.equipment_details.image_url is not None
    assert view.equipment_details.source_url.startswith("https://")


@pytest.mark.asyncio
async def test_inactive_preset_key_is_not_silently_reseeded() -> None:
    preset_keys = [item[0] for item in catalog_preset_definitions()]
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=ScalarRows(preset_keys)),
        add_all=Mock(),
    )

    changed = await ensure_business_catalog_presets(session, BUSINESS_ID)

    assert changed is False
    session.add_all.assert_not_called()

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.models import Business, BusinessCatalogItem, Service
from app.operations.service import OperationalService


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


def company(**changes: Any) -> Business:
    item = Business(
        id=BUSINESS_ID,
        name="Empresa",
        responsible_name="Responsável",
        timezone="America/Sao_Paulo",
        service_origin_address="Base operacional",
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

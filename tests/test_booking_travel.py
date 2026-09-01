import pytest

from app.booking.domain import ServiceAddress
from app.booking.travel import ConfiguredTravelTimePort

pytestmark = pytest.mark.asyncio


async def test_configured_region_rule_is_deterministic() -> None:
    port = ConfiguredTravelTimePort(
        default_minutes=35,
        region_rules=[{"match": "Jacareí", "minutes": 50, "served": True}],
    )

    estimate = await port.estimate(
        "Base operacional editável",
        ServiceAddress("Rua A", city="Jacareí", state="SP"),
    )

    assert estimate.travel_minutes == 50
    assert estimate.method == "region_rule"
    assert estimate.estimated is True
    assert estimate.within_service_area is True


async def test_default_fallback_and_operational_origin_are_configurable() -> None:
    port = ConfiguredTravelTimePort(default_minutes=28, region_rules=[])

    estimate = await port.estimate(
        "Zona Sul configurada",
        ServiceAddress("Rua B, São José dos Campos - SP"),
    )

    assert estimate.travel_minutes == 28
    assert estimate.method == "default_fallback"


async def test_region_can_be_marked_outside_service_area() -> None:
    port = ConfiguredTravelTimePort(
        default_minutes=30,
        region_rules=[{"match": "fora da área", "minutes": 90, "served": False}],
    )

    estimate = await port.estimate(
        "Zona Leste de São José dos Campos - SP",
        ServiceAddress("Endereço fora da área"),
    )

    assert estimate.within_service_area is False
    assert estimate.travel_minutes == 90

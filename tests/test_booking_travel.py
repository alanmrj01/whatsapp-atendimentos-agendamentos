import pytest

from app.booking.domain import ServiceAddress, TravelOrigin
from app.booking.travel import ConfiguredTravelTimePort

pytestmark = pytest.mark.asyncio


async def test_configured_region_rule_is_deterministic() -> None:
    port = ConfiguredTravelTimePort(
        fallback_minutes=None,
        fallback_allowed=False,
        region_rules=[
            {
                "match": "Jacareí",
                "minutes": 50,
                "served": True,
                "trusted": True,
            }
        ],
    )

    estimate = await port.estimate(
        TravelOrigin("Base operacional editável", is_precise=True),
        ServiceAddress("Rua A", city="Jacareí", state="SP"),
    )

    assert estimate.travel_minutes == 50
    assert estimate.method == "region_rule"
    assert estimate.estimated is True
    assert estimate.within_service_area is True


async def test_fallback_requires_explicit_business_permission() -> None:
    port = ConfiguredTravelTimePort(
        fallback_minutes=28,
        fallback_allowed=True,
        region_rules=[],
    )

    estimate = await port.estimate(
        TravelOrigin("Zona Sul configurada"),
        ServiceAddress("Rua B, São José dos Campos - SP"),
    )

    assert estimate.travel_minutes == 28
    assert estimate.method == "configured_fallback"
    assert estimate.available is True
    assert estimate.origin_is_precise is False


async def test_missing_trustworthy_rule_fails_closed_without_fallback() -> None:
    port = ConfiguredTravelTimePort(
        fallback_minutes=30,
        fallback_allowed=False,
        region_rules=[{"match": "Jacareí", "minutes": 15}],
    )

    estimate = await port.estimate(
        TravelOrigin("Origem aproximada"),
        ServiceAddress("Rua C", city="Jacareí", state="SP"),
    )

    assert estimate.available is False
    assert estimate.method == "unavailable"
    assert estimate.travel_minutes == 0


async def test_region_can_be_marked_outside_service_area() -> None:
    port = ConfiguredTravelTimePort(
        fallback_minutes=None,
        fallback_allowed=False,
        region_rules=[
            {
                "match": "fora da área",
                "minutes": 90,
                "served": False,
                "trusted": True,
            }
        ],
    )

    estimate = await port.estimate(
        TravelOrigin("Zona Leste de São José dos Campos - SP"),
        ServiceAddress("Endereço fora da área"),
    )

    assert estimate.within_service_area is False
    assert estimate.travel_minutes == 90

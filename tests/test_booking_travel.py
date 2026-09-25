from decimal import Decimal

import pytest

from app.booking.domain import ServiceAddress, TravelOrigin
from app.booking.travel import (
    ConfiguredTravelTimePort,
    GoogleRoutesTravelTimePort,
)

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



async def test_google_routes_returns_real_duration_distance_and_reuses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Credentials:
        valid = True
        expired = False
        token = "oauth-token"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "routes": [
                    {
                        "duration": "721s",
                        "distanceMeters": 8450,
                    }
                ]
            }

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> Response:
            calls.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr("app.booking.travel.httpx.AsyncClient", Client)
    port = GoogleRoutesTravelTimePort("whatsapp-automacao-prod")
    port._credentials = Credentials()  # type: ignore[attr-defined]

    origin = TravelOrigin(
        "Rua Itumbiara, 160 - Parque Industrial, São José dos Campos - SP"
    )
    destination = ServiceAddress(
        "Rua Maurício Cardoso, 201 - Jardim Sul, São José dos Campos - SP"
    )

    first = await port.estimate(origin, destination)
    second = await port.estimate(origin, destination)

    assert first.travel_minutes == 13
    assert first.distance_km == Decimal("8.45")
    assert first.source == "google_routes"
    assert first.method == "route"
    assert first.available is True
    assert second == first
    assert len(calls) == 1
    assert calls[0]["url"].endswith("directions/v2:computeRoutes")
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Goog-User-Project"] == "whatsapp-automacao-prod"
    assert "routes.duration" in headers["X-Goog-FieldMask"]


async def test_google_routes_fails_closed_without_inventing_travel_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Credentials:
        valid = True
        expired = False
        token = "oauth-token"

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.booking.travel.httpx.AsyncClient", Client)
    port = GoogleRoutesTravelTimePort("whatsapp-automacao-prod")
    port._credentials = Credentials()  # type: ignore[attr-defined]

    estimate = await port.estimate(
        TravelOrigin("Origem"),
        ServiceAddress("Destino"),
    )

    assert estimate.available is False
    assert estimate.travel_minutes == 0
    assert estimate.method == "unavailable"

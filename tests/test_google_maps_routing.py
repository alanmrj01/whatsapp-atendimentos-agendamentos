from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.booking.domain import (
    AddressResolution,
    AddressResolutionStatus,
    ServiceAddress,
    TravelOrigin,
)
from app.booking.google_maps import (
    GoogleRoutesTravelTimePort,
    address_resolution_from_google_response,
)


def google_address_response(
    *,
    action: str = "ACCEPT",
    complete: bool = True,
    granularity: str = "PREMISE",
    place_id: str = "ChIJ-test",
    missing: list[str] | None = None,
    inferred: bool = False,
) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "possibleNextAction": action,
        "addressComplete": complete,
        "validationGranularity": granularity,
    }
    if inferred:
        verdict["hasInferredComponents"] = True
    return {
        "result": {
            "verdict": verdict,
            "address": {
                "missingComponentTypes": missing or [],
            },
            "geocode": {
                "placeId": place_id,
            },
        }
    }


def test_accepts_premise_even_when_postal_code_was_inferred() -> None:
    resolution = address_resolution_from_google_response(
        google_address_response(inferred=True),
        raw_address="Rua Mauricio Cardoso, 201 - Jardim Sul",
        default_city="São José dos Campos",
        default_state="SP",
    )

    assert resolution.status is AddressResolutionStatus.ACCEPTED
    assert resolution.address is not None
    assert resolution.address.address_line == (
        "Rua Mauricio Cardoso, 201 - Jardim Sul"
    )
    assert resolution.address.city == "São José dos Campos"
    assert resolution.address.state == "SP"
    assert resolution.address.place_id == "ChIJ-test"


def test_missing_street_number_requests_only_street_number() -> None:
    resolution = address_resolution_from_google_response(
        google_address_response(
            action="FIX",
            complete=False,
            granularity="ROUTE",
            missing=["street_number"],
        ),
        raw_address="Rua Mauricio Cardoso - Jardim Sul",
        default_city="São José dos Campos",
        default_state="SP",
    )

    assert resolution.status is AddressResolutionStatus.NEEDS_INPUT
    assert resolution.missing_component == "street_number"
    assert resolution.address is None


def test_confirm_signal_keeps_place_id_for_customer_confirmation() -> None:
    resolution = address_resolution_from_google_response(
        google_address_response(action="CONFIRM"),
        raw_address="Rua Exemplo, 10",
        default_city="São José dos Campos",
        default_state="SP",
    )

    assert resolution.status is AddressResolutionStatus.NEEDS_CONFIRMATION
    assert resolution.address is not None
    assert resolution.address.place_id == "ChIJ-test"


class FakeAddressValidationPort:
    async def resolve(
        self,
        raw_address: str,
        *,
        default_city: str | None = None,
        default_state: str | None = None,
    ) -> AddressResolution:
        return AddressResolution(
            AddressResolutionStatus.ACCEPTED,
            address=ServiceAddress(
                address_line=raw_address,
                city=default_city,
                state=default_state,
                place_id="origin-place-id",
            ),
        )


class FakeRoutesClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, Any], str | None]] = []

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        field_mask: str | None = None,
    ) -> dict[str, Any]:
        self.requests.append((url, payload, field_mask))
        return self.response


@pytest.mark.asyncio
async def test_routes_adapter_uses_place_ids_and_static_duration() -> None:
    port = GoogleRoutesTravelTimePort(
        project_id="whatsapp-automacao-prod",
        token_provider=object(),  # type: ignore[arg-type]
        address_validation_port=FakeAddressValidationPort(),
    )
    fake_client = FakeRoutesClient(
        {
            "routes": [
                {
                    "distanceMeters": 2715,
                    "duration": "453s",
                    "staticDuration": "453s",
                }
            ]
        }
    )
    port.client = fake_client  # type: ignore[assignment]

    estimate = await port.estimate(
        TravelOrigin(
            address=(
                "Rua Itumbiara, 160 - Parque Industrial, "
                "São José dos Campos - SP, CEP 12235-740"
            )
        ),
        ServiceAddress(
            address_line="Rua Mauricio Cardoso, 201 - Jardim Sul",
            place_id="destination-place-id",
        ),
    )

    assert estimate.available is True
    assert estimate.travel_minutes == 8
    assert estimate.distance_km == Decimal("2.715")
    assert estimate.source == "google_routes"
    assert estimate.method == "static_drive_route"
    assert estimate.failure_reason is None

    _, payload, field_mask = fake_client.requests[0]
    assert payload["origin"] == {"placeId": "origin-place-id"}
    assert payload["destination"] == {"placeId": "destination-place-id"}
    assert payload["routingPreference"] == "TRAFFIC_UNAWARE"
    assert field_mask == (
        "routes.distanceMeters,routes.duration,routes.staticDuration"
    )

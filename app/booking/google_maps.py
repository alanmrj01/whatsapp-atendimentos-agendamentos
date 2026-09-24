from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal
from typing import Any, Protocol

import google.auth
import httpx
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from app.booking.domain import (
    AddressResolution,
    AddressResolutionStatus,
    ServiceAddress,
    TravelEstimate,
    TravelOrigin,
)
from app.booking.travel import (
    same_address_travel_estimate,
    unavailable_travel_estimate,
)

logger = logging.getLogger(__name__)

_ADDRESS_VALIDATION_URL = (
    "https://addressvalidation.googleapis.com/v1:validateAddress"
)
_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_PREMISE_GRANULARITIES = {"PREMISE", "SUB_PREMISE"}
_MISSING_COMPONENT_PRIORITY = (
    "street_number",
    "route",
    "locality",
    "administrative_area",
    "postal_code",
)


class AddressValidationPort(Protocol):
    async def resolve(
        self,
        raw_address: str,
        *,
        default_city: str | None = None,
        default_state: str | None = None,
    ) -> AddressResolution: ...


class GoogleMapsAccessTokenProvider:
    """ADC-backed short-lived OAuth tokens for server-to-server Maps calls."""

    def __init__(self, credentials: Credentials | None = None) -> None:
        if credentials is None:
            credentials, _ = google.auth.default(
                scopes=("https://www.googleapis.com/auth/cloud-platform",)
            )
        self._credentials = credentials
        self._lock = asyncio.Lock()

    async def get_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if (
                force_refresh
                or not self._credentials.valid
                or not self._credentials.token
            ):
                await asyncio.to_thread(
                    self._credentials.refresh,
                    Request(),
                )
            token = self._credentials.token
            if not token:
                raise RuntimeError("Google OAuth token is unavailable")
            return str(token)


class _GoogleMapsJsonClient:
    def __init__(
        self,
        *,
        project_id: str,
        token_provider: GoogleMapsAccessTokenProvider,
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
    ) -> None:
        self.project_id = project_id
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        field_mask: str | None = None,
    ) -> dict[str, Any] | None:
        force_refresh = False
        for attempt in range(self.max_attempts):
            try:
                token = await self.token_provider.get_token(
                    force_refresh=force_refresh
                )
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Goog-User-Project": self.project_id,
                    "Content-Type": "application/json",
                }
                if field_mask:
                    headers["X-Goog-FieldMask"] = field_mask
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds
                ) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                    )
            except (httpx.TransportError, TimeoutError, RuntimeError) as exc:
                logger.warning(
                    "google_maps_request_transport_failure",
                    extra={"error_type": type(exc).__name__},
                )
                if attempt + 1 >= self.max_attempts:
                    return None
                await asyncio.sleep(0.25 * (2**attempt))
                continue

            if response.status_code == 401 and not force_refresh:
                force_refresh = True
                continue
            if response.status_code in _RETRYABLE_HTTP_STATUSES:
                if attempt + 1 >= self.max_attempts:
                    return None
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code >= 400:
                logger.warning(
                    "google_maps_request_rejected",
                    extra={"status_code": response.status_code},
                )
                return None
            try:
                data = response.json()
            except ValueError:
                logger.warning("google_maps_invalid_json_response")
                return None
            return data if isinstance(data, dict) else None
        return None


class GoogleAddressValidationPort:
    def __init__(
        self,
        *,
        project_id: str,
        token_provider: GoogleMapsAccessTokenProvider,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.client = _GoogleMapsJsonClient(
            project_id=project_id,
            token_provider=token_provider,
            timeout_seconds=timeout_seconds,
        )

    async def resolve(
        self,
        raw_address: str,
        *,
        default_city: str | None = None,
        default_state: str | None = None,
    ) -> AddressResolution:
        postal_address: dict[str, Any] = {
            "regionCode": "BR",
            "languageCode": "pt-BR",
            "addressLines": [raw_address.strip()],
        }
        if default_city and default_city.strip():
            postal_address["locality"] = default_city.strip()
        if default_state and default_state.strip():
            postal_address["administrativeArea"] = default_state.strip()

        data = await self.client.post_json(
            _ADDRESS_VALIDATION_URL,
            {"address": postal_address},
        )
        if data is None:
            return AddressResolution(
                AddressResolutionStatus.TEMPORARILY_UNAVAILABLE,
                reason="address_validation_temporarily_unavailable",
            )
        return address_resolution_from_google_response(
            data,
            raw_address=raw_address,
            default_city=default_city,
            default_state=default_state,
        )


class GoogleRoutesTravelTimePort:
    def __init__(
        self,
        *,
        project_id: str,
        token_provider: GoogleMapsAccessTokenProvider,
        address_validation_port: AddressValidationPort,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.client = _GoogleMapsJsonClient(
            project_id=project_id,
            token_provider=token_provider,
            timeout_seconds=timeout_seconds,
        )
        self.address_validation_port = address_validation_port
        self._origin_place_ids: dict[str, str] = {}

    async def estimate(
        self,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate:
        same_address = same_address_travel_estimate(origin, destination)
        if same_address is not None:
            return same_address

        origin_place_id = self._origin_place_ids.get(origin.address)
        if origin_place_id is None:
            origin_resolution = await self.address_validation_port.resolve(
                origin.address
            )
            if (
                origin_resolution.status is not AddressResolutionStatus.ACCEPTED
                or origin_resolution.address is None
                or not origin_resolution.address.place_id
            ):
                return unavailable_travel_estimate(
                    origin,
                    reason="origin_address_unavailable",
                    source="google_address_validation",
                    method="address_validation",
                )
            origin_place_id = origin_resolution.address.place_id
            self._origin_place_ids[origin.address] = origin_place_id

        destination_place_id = destination.place_id
        if not destination_place_id:
            destination_resolution = await self.address_validation_port.resolve(
                destination.searchable_text,
                default_city=destination.city,
                default_state=destination.state,
            )
            if (
                destination_resolution.status
                is not AddressResolutionStatus.ACCEPTED
                or destination_resolution.address is None
                or not destination_resolution.address.place_id
            ):
                return unavailable_travel_estimate(
                    origin,
                    reason="destination_address_unavailable",
                    source="google_address_validation",
                    method="address_validation",
                )
            destination_place_id = destination_resolution.address.place_id

        data = await self.client.post_json(
            _ROUTES_URL,
            {
                "origin": {"placeId": origin_place_id},
                "destination": {"placeId": destination_place_id},
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_UNAWARE",
                "languageCode": "pt-BR",
                "units": "METRIC",
            },
            field_mask=(
                "routes.distanceMeters,routes.duration,"
                "routes.staticDuration"
            ),
        )
        if data is None:
            return unavailable_travel_estimate(
                origin,
                reason="route_temporarily_unavailable",
                source="google_routes",
                method="compute_routes",
            )

        routes = data.get("routes")
        if not isinstance(routes, list) or not routes:
            return unavailable_travel_estimate(
                origin,
                reason="route_not_found",
                source="google_routes",
                method="compute_routes",
            )
        route = routes[0]
        if not isinstance(route, dict):
            return unavailable_travel_estimate(
                origin,
                reason="route_not_found",
                source="google_routes",
                method="compute_routes",
            )

        duration_seconds = _duration_seconds(
            route.get("staticDuration") or route.get("duration")
        )
        distance_meters = route.get("distanceMeters")
        if duration_seconds is None or not isinstance(distance_meters, int):
            return unavailable_travel_estimate(
                origin,
                reason="route_invalid_response",
                source="google_routes",
                method="compute_routes",
            )

        return TravelEstimate(
            travel_minutes=max(0, math.ceil(duration_seconds / 60)),
            distance_km=(
                Decimal(distance_meters) / Decimal("1000")
                if distance_meters >= 0
                else None
            ),
            source="google_routes",
            method="static_drive_route",
            estimated=False,
            origin_is_precise=True,
        )


def address_resolution_from_google_response(
    data: dict[str, Any],
    *,
    raw_address: str,
    default_city: str | None = None,
    default_state: str | None = None,
) -> AddressResolution:
    result = data.get("result")
    if not isinstance(result, dict):
        return AddressResolution(
            AddressResolutionStatus.TEMPORARILY_UNAVAILABLE,
            reason="address_validation_invalid_response",
        )
    verdict = result.get("verdict")
    address_data = result.get("address")
    geocode = result.get("geocode")
    if not isinstance(verdict, dict) or not isinstance(address_data, dict):
        return AddressResolution(
            AddressResolutionStatus.TEMPORARILY_UNAVAILABLE,
            reason="address_validation_invalid_response",
        )
    geocode = geocode if isinstance(geocode, dict) else {}

    missing = address_data.get("missingComponentTypes")
    missing_components = (
        [item for item in missing if isinstance(item, str)]
        if isinstance(missing, list)
        else []
    )
    missing_component = _preferred_missing_component(missing_components)
    if missing_component is not None:
        return AddressResolution(
            AddressResolutionStatus.NEEDS_INPUT,
            missing_component=missing_component,
            reason="address_missing_component",
        )

    place_id = geocode.get("placeId")
    place_id = place_id.strip() if isinstance(place_id, str) else ""
    action = verdict.get("possibleNextAction")
    granularity = verdict.get("validationGranularity")
    complete = verdict.get("addressComplete") is True

    if action == "FIX" or not place_id:
        return AddressResolution(
            AddressResolutionStatus.NEEDS_INPUT,
            missing_component="generic",
            reason="address_requires_fix",
        )

    resolved = ServiceAddress(
        address_line=raw_address.strip(),
        city=default_city,
        state=default_state,
        place_id=place_id,
    )

    if (
        action == "ACCEPT"
        and complete
        and granularity in _PREMISE_GRANULARITIES
    ):
        return AddressResolution(
            AddressResolutionStatus.ACCEPTED,
            address=resolved,
        )

    if action == "ACCEPT" and complete and granularity == "PREMISE_PROXIMITY":
        return AddressResolution(
            AddressResolutionStatus.NEEDS_CONFIRMATION,
            address=resolved,
            reason="address_premise_proximity",
        )

    if action in {"CONFIRM", "CONFIRM_ADD_SUBPREMISES"}:
        return AddressResolution(
            AddressResolutionStatus.NEEDS_CONFIRMATION,
            address=resolved,
            reason="address_requires_confirmation",
        )

    if complete and granularity in _PREMISE_GRANULARITIES:
        return AddressResolution(
            AddressResolutionStatus.ACCEPTED,
            address=resolved,
        )

    return AddressResolution(
        AddressResolutionStatus.NEEDS_INPUT,
        missing_component="generic",
        reason="address_not_precise_enough",
    )


def _preferred_missing_component(values: list[str]) -> str | None:
    normalized = set(values)
    for component in _MISSING_COMPONENT_PRIORITY:
        if component in normalized:
            return component
    return "generic" if normalized else None


def _duration_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = float(value[:-1])
    except ValueError:
        return None
    return seconds if seconds >= 0 else None

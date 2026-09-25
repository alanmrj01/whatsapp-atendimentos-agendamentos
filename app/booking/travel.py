from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import unicodedata
from decimal import Decimal
from typing import Protocol

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
import httpx

from app.booking.domain import ServiceAddress, TravelEstimate, TravelOrigin

logger = logging.getLogger(__name__)

_GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_GOOGLE_ROUTES_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_ROUTE_CACHE_TTL_SECONDS = 15 * 60
_ROUTE_CACHE_LIMIT = 2048


class TravelTimePort(Protocol):
    async def estimate(
        self,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate: ...


def same_address_travel_estimate(
    origin: TravelOrigin,
    destination: ServiceAddress,
) -> TravelEstimate | None:
    """Return zero travel only when the two textual addresses are safely equivalent.

    The comparison is intentionally conservative: it ignores accents, punctuation,
    token order and CEP formatting, but still requires a shared street number and
    enough matching address tokens before treating the locations as identical.
    """

    origin_tokens = _address_tokens(origin.address)
    destination_tokens = _address_tokens(destination.searchable_text)
    if not origin_tokens or not destination_tokens:
        return None

    shared_numbers = {
        token for token in origin_tokens if token.isdigit()
    } & {
        token for token in destination_tokens if token.isdigit()
    }
    if not shared_numbers:
        return None

    if origin_tokens == destination_tokens:
        equivalent = True
    else:
        smaller, larger = (
            (origin_tokens, destination_tokens)
            if len(origin_tokens) <= len(destination_tokens)
            else (destination_tokens, origin_tokens)
        )
        descriptive_tokens = [
            token
            for token in smaller
            if token.isalpha() and len(token) >= 5
        ]
        equivalent = (
            len(smaller) >= 5
            and smaller.issubset(larger)
            and len(descriptive_tokens) >= 2
        )

    if not equivalent:
        return None
    return TravelEstimate(
        travel_minutes=0,
        distance_km=None,
        source="same_address",
        method="same_address",
        estimated=False,
        origin_is_precise=origin.is_precise,
    )


class GoogleRoutesTravelTimePort:
    """Calcula deslocamento real pela Google Routes API usando ADC do Cloud Run.

    Usa TRAFFIC_UNAWARE para manter custo e comportamento previsíveis no
    planejamento. O cache evita repetir a mesma rota durante a montagem da agenda.
    """

    def __init__(
        self,
        project_id: str,
        *,
        request_timeout_seconds: float = 6.0,
    ) -> None:
        if not project_id.strip():
            raise ValueError("Google Routes project id is required")
        self.project_id = project_id.strip()
        self.request_timeout_seconds = request_timeout_seconds
        self._credentials = None
        self._credential_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._cache: dict[
            tuple[str, str],
            tuple[float, TravelEstimate],
        ] = {}

    async def estimate(
        self,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate:
        same_address = same_address_travel_estimate(origin, destination)
        if same_address is not None:
            return same_address

        cache_key = (
            _travel_location_key(origin),
            _normalize(destination.searchable_text),
        )
        cached = await self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            token = await self._access_token()
            async with httpx.AsyncClient(
                timeout=self.request_timeout_seconds
            ) as client:
                response = await client.post(
                    _GOOGLE_ROUTES_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "X-Goog-FieldMask": (
                            "routes.duration,routes.distanceMeters"
                        ),
                        "X-Goog-User-Project": self.project_id,
                    },
                    json={
                        "origin": _origin_waypoint(origin),
                        "destination": {
                            "address": destination.searchable_text,
                        },
                        "travelMode": "DRIVE",
                        "routingPreference": "TRAFFIC_UNAWARE",
                        "languageCode": "pt-BR",
                        "regionCode": "BR",
                        "units": "METRIC",
                    },
                )
            response.raise_for_status()
            payload = response.json()
            routes = payload.get("routes")
            if not isinstance(routes, list) or not routes:
                raise ValueError("Routes API returned no route")
            first = routes[0]
            if not isinstance(first, dict):
                raise ValueError("Routes API returned invalid route")
            duration_seconds = _duration_seconds(first.get("duration"))
            distance_meters = first.get("distanceMeters")
            if (
                duration_seconds is None
                or not isinstance(distance_meters, int)
                or isinstance(distance_meters, bool)
                or distance_meters < 0
            ):
                raise ValueError("Routes API returned incomplete route")
            estimate = TravelEstimate(
                travel_minutes=max(1, math.ceil(duration_seconds / 60)),
                distance_km=(
                    Decimal(distance_meters) / Decimal("1000")
                ).quantize(Decimal("0.01")),
                source="google_routes",
                method="route",
                estimated=True,
                origin_is_precise=origin.is_precise,
            )
        except Exception as exc:
            logger.warning(
                "google_routes_estimate_failed",
                extra={"error_type": type(exc).__name__},
            )
            return unavailable_travel_estimate(origin)

        await self._store_cache(cache_key, estimate)
        return estimate

    async def _access_token(self) -> str:
        async with self._credential_lock:
            if self._credentials is None:
                credentials, _ = await asyncio.to_thread(
                    google.auth.default,
                    scopes=[_GOOGLE_ROUTES_SCOPE],
                )
                self._credentials = credentials
            credentials = self._credentials
            if (
                not getattr(credentials, "valid", False)
                or getattr(credentials, "expired", True)
                or not getattr(credentials, "token", None)
            ):
                await asyncio.to_thread(
                    credentials.refresh,
                    GoogleAuthRequest(),
                )
            token = getattr(credentials, "token", None)
            if not isinstance(token, str) or not token:
                raise RuntimeError("Google Routes OAuth token unavailable")
            return token

    async def _cached(
        self,
        key: tuple[str, str],
    ) -> TravelEstimate | None:
        now = time.monotonic()
        async with self._cache_lock:
            item = self._cache.get(key)
            if item is None:
                return None
            expires_at, estimate = item
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            return estimate

    async def _store_cache(
        self,
        key: tuple[str, str],
        estimate: TravelEstimate,
    ) -> None:
        async with self._cache_lock:
            if len(self._cache) >= _ROUTE_CACHE_LIMIT:
                oldest_key = min(
                    self._cache,
                    key=lambda cached_key: self._cache[cached_key][0],
                )
                self._cache.pop(oldest_key, None)
            self._cache[key] = (
                time.monotonic() + _ROUTE_CACHE_TTL_SECONDS,
                estimate,
            )


def _origin_waypoint(origin: TravelOrigin) -> dict[str, object]:
    if origin.latitude is not None and origin.longitude is not None:
        return {
            "location": {
                "latLng": {
                    "latitude": float(origin.latitude),
                    "longitude": float(origin.longitude),
                }
            }
        }
    return {"address": origin.address}


def _travel_location_key(origin: TravelOrigin) -> str:
    if origin.latitude is not None and origin.longitude is not None:
        return f"{origin.latitude}:{origin.longitude}"
    return _normalize(origin.address)


def _duration_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = float(value[:-1])
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class ConfiguredTravelTimePort:
    """Estimativa explícita baseada somente em regras confiáveis do business."""

    def __init__(
        self,
        *,
        fallback_minutes: int | None,
        fallback_allowed: bool,
        region_rules: list[dict[str, object]],
    ) -> None:
        if fallback_minutes is not None and fallback_minutes < 0:
            raise ValueError("Fallback travel time cannot be negative")
        if fallback_allowed and fallback_minutes is None:
            raise ValueError("Allowed fallback requires configured minutes")
        self.fallback_minutes = fallback_minutes
        self.fallback_allowed = fallback_allowed
        self.region_rules = tuple(region_rules)

    async def estimate(
        self,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate:
        same_address = same_address_travel_estimate(origin, destination)
        if same_address is not None:
            return same_address
        destination_text = _normalize(destination.searchable_text)
        for rule in self.region_rules:
            match = rule.get("match")
            minutes = rule.get("minutes")
            if (
                rule.get("trusted") is not True
                or not isinstance(match, str)
                or not match.strip()
                or not isinstance(minutes, int)
                or isinstance(minutes, bool)
                or minutes < 0
            ):
                continue
            if _normalize(match) not in destination_text:
                continue
            served = rule.get("served", True)
            return TravelEstimate(
                travel_minutes=minutes,
                distance_km=None,
                source="business_configuration",
                method="region_rule",
                estimated=True,
                within_service_area=served is not False,
                origin_is_precise=origin.is_precise,
            )
        if not self.fallback_allowed or self.fallback_minutes is None:
            return unavailable_travel_estimate(origin)
        return TravelEstimate(
            travel_minutes=self.fallback_minutes,
            distance_km=None,
            source="business_configuration",
            method="configured_fallback",
            estimated=True,
            origin_is_precise=origin.is_precise,
        )


def unavailable_travel_estimate(origin: TravelOrigin) -> TravelEstimate:
    return TravelEstimate(
        travel_minutes=0,
        distance_km=None,
        source="configuration_unavailable",
        method="unavailable",
        estimated=True,
        available=False,
        origin_is_precise=origin.is_precise,
    )


def _address_tokens(value: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    without_postal_code = re.sub(
        r"\b\d{5}\s*-?\s*\d{3}\b",
        " ",
        without_accents,
    )
    aliases = {
        "r": "rua",
        "av": "avenida",
        "avda": "avenida",
        "rod": "rodovia",
    }
    return {
        aliases.get(token, token)
        for token in re.findall(r"[a-z0-9]+", without_postal_code)
        if token != "cep"
    }


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))

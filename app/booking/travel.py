from __future__ import annotations

import unicodedata
from typing import Protocol

from app.booking.domain import ServiceAddress, TravelEstimate, TravelOrigin


class TravelTimePort(Protocol):
    async def estimate(
        self,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate: ...


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


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))

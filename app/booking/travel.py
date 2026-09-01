from __future__ import annotations

import unicodedata
from typing import Protocol

from app.booking.domain import ServiceAddress, TravelEstimate


class TravelTimePort(Protocol):
    async def estimate(
        self,
        origin: str,
        destination: ServiceAddress,
    ) -> TravelEstimate: ...


class ConfiguredTravelTimePort:
    """Fallback determinístico baseado em regras editáveis do business."""

    def __init__(
        self,
        *,
        default_minutes: int,
        region_rules: list[dict[str, object]],
    ) -> None:
        if default_minutes < 0:
            raise ValueError("Default travel time cannot be negative")
        self.default_minutes = default_minutes
        self.region_rules = tuple(region_rules)

    async def estimate(
        self,
        origin: str,
        destination: ServiceAddress,
    ) -> TravelEstimate:
        if not origin.strip():
            raise ValueError("Operational origin is required")
        destination_text = _normalize(destination.searchable_text)
        for rule in self.region_rules:
            match = rule.get("match")
            minutes = rule.get("minutes")
            if (
                not isinstance(match, str)
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
            )
        return TravelEstimate(
            travel_minutes=self.default_minutes,
            distance_km=None,
            source="business_configuration",
            method="default_fallback",
            estimated=True,
        )


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))

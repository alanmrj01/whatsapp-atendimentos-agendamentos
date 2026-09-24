from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from app.booking.domain import ServiceAddress, TravelEstimate, TravelOrigin


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

    if origin_tokens == destination_tokens:
        equivalent = True
    else:
        smaller, larger = (
            (origin_tokens, destination_tokens)
            if len(origin_tokens) <= len(destination_tokens)
            else (destination_tokens, origin_tokens)
        )
        shared_numbers = {
            token for token in smaller if token.isdigit()
        } & {
            token for token in larger if token.isdigit()
        }
        descriptive_tokens = [
            token
            for token in smaller
            if token.isalpha() and len(token) >= 5
        ]
        equivalent = (
            len(smaller) >= 5
            and smaller.issubset(larger)
            and bool(shared_numbers)
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

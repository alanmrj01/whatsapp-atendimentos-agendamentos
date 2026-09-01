from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from enum import StrEnum
from typing import Any


class PricingType(StrEnum):
    FIXED = "fixed"
    ESTIMATED = "estimated"
    HUMAN_QUOTE = "human_quote"


class AccessCondition(StrEnum):
    NORMAL = "normal"
    DIFFICULT = "difficult"
    UNKNOWN = "unknown"


class UnknownAccessPolicy(StrEnum):
    STANDARD = "standard"
    CONSERVATIVE = "conservative"
    HUMAN_QUOTE = "human_quote"


@dataclass(frozen=True, slots=True)
class ServiceAddress:
    address_line: str
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None

    def __post_init__(self) -> None:
        if not self.address_line.strip():
            raise ValueError("Service address cannot be empty")

    @property
    def searchable_text(self) -> str:
        return " ".join(
            value.strip()
            for value in (
                self.address_line,
                self.number,
                self.complement,
                self.neighborhood,
                self.city,
                self.state,
                self.postal_code,
            )
            if value and value.strip()
        )

    def to_snapshot(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "address_line": self.address_line.strip(),
                "number": self.number,
                "complement": self.complement,
                "neighborhood": self.neighborhood,
                "city": self.city,
                "state": self.state,
                "postal_code": self.postal_code,
            }.items()
            if value is not None and value.strip()
        }

    @classmethod
    def from_snapshot(cls, value: object) -> ServiceAddress | None:
        if not isinstance(value, dict):
            return None
        address_line = value.get("address_line")
        if not isinstance(address_line, str) or not address_line.strip():
            return None
        optional = {
            key: raw if isinstance(raw, str) and raw.strip() else None
            for key in (
                "number",
                "complement",
                "neighborhood",
                "city",
                "state",
                "postal_code",
            )
            if (raw := value.get(key)) is not None
        }
        return cls(address_line=address_line, **optional)


@dataclass(frozen=True, slots=True)
class BookingRequirements:
    quantity: int | None = 1
    access_condition: AccessCondition = AccessCondition.NORMAL
    address: ServiceAddress | None = None
    site_allowed_end: time | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("Idempotency key cannot be empty")


@dataclass(frozen=True, slots=True)
class ServiceConfiguration:
    duration_minutes: int
    base_price: Decimal | None
    pricing_type: PricingType
    automatic_booking: bool
    included_quantity: int
    additional_unit_duration_minutes: int
    additional_unit_price: Decimal | None
    requires_address: bool
    requires_quantity: bool
    considers_difficult_access: bool
    difficult_access_duration_minutes: int
    difficult_access_price: Decimal | None
    unknown_access_policy: UnknownAccessPolicy
    duration_margin_minutes: int


@dataclass(frozen=True, slots=True)
class ServiceIntake:
    requires_quantity: bool
    requires_address: bool
    considers_difficult_access: bool
    asks_site_time_limit: bool
    automatic_booking: bool
    pricing_type: PricingType


@dataclass(frozen=True, slots=True)
class ServiceEstimate:
    estimated_duration_minutes: int
    estimated_price: Decimal | None
    pricing_type: PricingType
    requires_human_quote: bool
    applied_rules: tuple[str, ...]
    qualifier: str


@dataclass(frozen=True, slots=True)
class TravelEstimate:
    travel_minutes: int
    distance_km: Decimal | None
    source: str
    method: str
    estimated: bool
    within_service_area: bool = True

    def __post_init__(self) -> None:
        if self.travel_minutes < 0:
            raise ValueError("Travel minutes cannot be negative")


@dataclass(frozen=True, slots=True)
class BookingPlan:
    service: ServiceEstimate
    travel: TravelEstimate
    travel_before_minutes: int
    travel_after_minutes: int
    requires_handoff: bool
    handoff_reason: str | None = None

    def __post_init__(self) -> None:
        if self.travel_before_minutes < 0 or self.travel_after_minutes < 0:
            raise ValueError("Travel reservation cannot be negative")

    def snapshot_details(self) -> dict[str, Any]:
        return {
            "applied_rules": list(self.service.applied_rules),
            "qualifier": self.service.qualifier,
            "travel_source": self.travel.source,
            "travel_method": self.travel.method,
            "travel_estimated": self.travel.estimated,
        }

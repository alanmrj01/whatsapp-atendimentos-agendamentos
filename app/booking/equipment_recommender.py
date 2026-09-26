from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Sequence

EquipmentPreference = Literal["modern", "cost_benefit", "economy"]
ClimateMode = Literal["cold", "heat_cool"]


@dataclass(frozen=True, slots=True)
class EquipmentCatalogEntry:
    item_id: str
    brand: str
    line: str
    capacity_btu: int
    segment: EquipmentPreference
    cycles: tuple[ClimateMode, ...]
    features: tuple[str, ...] = ()
    indoor_dimensions_cm: dict[str, float] | None = None
    outdoor_dimensions_cm: dict[str, float] | None = None
    condenser_form: str | None = None
    image_url: str | None = None
    source_url: str = ""
    price: float | None = None


@dataclass(frozen=True, slots=True)
class EquipmentRecommendation:
    item_id: str
    brand: str
    line: str
    capacity_btu: int
    segment: EquipmentPreference
    cycles: tuple[ClimateMode, ...]
    features: tuple[str, ...]
    source_url: str
    image_url: str | None
    condenser_form: str | None
    price: float | None
    required_btu: int

    @property
    def label(self) -> str:
        return f"{self.brand} {self.line} {self.capacity_btu:,} BTU".replace(",", ".")


@lru_cache(maxsize=1)
def equipment_catalog() -> tuple[dict[str, object], ...]:
    path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "equipment_recommendation_catalog.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("Equipment recommendation catalog is invalid")
    items: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("active") is not True:
            continue
        capacity = item.get("capacity_btu")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            continue
        items.append(item)
    if not items:
        raise RuntimeError("Equipment recommendation catalog is empty")
    return tuple(items)


def required_capacity_btu(area_m2: float, people: int) -> int:
    """Commercial reference only; not a full thermal-load calculation."""

    if area_m2 <= 0 or area_m2 > 500:
        raise ValueError("Area must be between 0 and 500 m²")
    if people <= 0 or people > 100:
        raise ValueError("People must be between 1 and 100")
    return max(9000, round((area_m2 * 600) + (max(0, people - 1) * 600)))


def default_catalog_entries() -> tuple[EquipmentCatalogEntry, ...]:
    return tuple(_entry_from_mapping(item) for item in equipment_catalog())


def recommend_equipment(
    area_m2: float,
    people: int,
    preference: EquipmentPreference,
    *,
    climate_mode: ClimateMode = "cold",
    indoor_space: tuple[float, float, float | None] | None = None,
    outdoor_space: tuple[float, float, float | None] | None = None,
    entries: Sequence[EquipmentCatalogEntry] | None = None,
) -> EquipmentRecommendation:
    required = required_capacity_btu(area_m2, people)
    catalog = tuple(entries or default_catalog_entries())
    if not catalog:
        raise ValueError("No active equipment is available")

    candidates = [
        item
        for item in catalog
        if climate_mode in item.cycles
        and _fits(item.indoor_dimensions_cm, indoor_space)
        and _fits(item.outdoor_dimensions_cm, outdoor_space)
    ]
    if not candidates:
        raise ValueError(
            "No active equipment matches the requested cycle and installation space"
        )

    minimum_acceptable = required * 0.90
    capacity_candidates = [
        item for item in candidates if item.capacity_btu >= minimum_acceptable
    ]
    if capacity_candidates:
        candidates = capacity_candidates

    preferred_capacity = min(item.capacity_btu for item in candidates)
    same_capacity = [
        item for item in candidates if item.capacity_btu == preferred_capacity
    ]

    segment_priority = {
        "modern": ("modern", "cost_benefit", "economy"),
        "cost_benefit": ("cost_benefit", "economy", "modern"),
        "economy": ("economy", "cost_benefit", "modern"),
    }[preference]
    segment_rank = {
        segment: index for index, segment in enumerate(segment_priority)
    }
    chosen = min(
        same_capacity,
        key=lambda item: (
            segment_rank.get(item.segment, 99),
            item.price if item.price is not None else float("inf"),
            item.brand,
            item.line,
        ),
    )
    return EquipmentRecommendation(
        item_id=chosen.item_id,
        brand=chosen.brand,
        line=chosen.line,
        capacity_btu=chosen.capacity_btu,
        segment=chosen.segment,
        cycles=chosen.cycles,
        features=chosen.features,
        source_url=chosen.source_url,
        image_url=chosen.image_url,
        condenser_form=chosen.condenser_form,
        price=chosen.price,
        required_btu=required,
    )


def _entry_from_mapping(item: dict[str, object]) -> EquipmentCatalogEntry:
    cycles_raw = item.get("cycles")
    cycles = tuple(
        value
        for value in cycles_raw
        if value in {"cold", "heat_cool"}
    ) if isinstance(cycles_raw, list) else ("cold",)
    segment_raw = str(item.get("segment") or "cost_benefit")
    segment: EquipmentPreference = (
        segment_raw
        if segment_raw in {"modern", "cost_benefit", "economy"}
        else "cost_benefit"
    )  # type: ignore[assignment]
    return EquipmentCatalogEntry(
        item_id=str(item["id"]),
        brand=str(item["brand"]),
        line=str(item["line"]),
        capacity_btu=int(item["capacity_btu"]),
        segment=segment,
        cycles=cycles,  # type: ignore[arg-type]
        features=tuple(
            str(value)
            for value in item.get("features", [])
            if isinstance(value, str)
        ) if isinstance(item.get("features"), list) else (),
        indoor_dimensions_cm=_dimension_dict(item.get("indoor_dimensions_cm")),
        outdoor_dimensions_cm=_dimension_dict(item.get("outdoor_dimensions_cm")),
        condenser_form=(
            str(item.get("condenser_form"))
            if item.get("condenser_form") is not None
            else None
        ),
        image_url=(
            str(item.get("image_url"))
            if isinstance(item.get("image_url"), str)
            else None
        ),
        source_url=str(item.get("source_url") or ""),
    )


def _dimension_dict(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, float] = {}
    for key in ("width", "height", "depth"):
        raw = value.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            result[key] = float(raw)
    return result if {"width", "height"} <= result.keys() else None


def _fits(
    equipment: dict[str, float] | None,
    available: tuple[float, float, float | None] | None,
) -> bool:
    if available is None:
        return True
    if equipment is None:
        # Unknown dimensions cannot be guaranteed to fit a declared restriction.
        return False
    width, height, depth = available
    if equipment["width"] > width or equipment["height"] > height:
        return False
    if depth is not None and equipment.get("depth", 0) > depth:
        return False
    return True

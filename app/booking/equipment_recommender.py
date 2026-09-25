from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

EquipmentPreference = Literal["modern", "cost_benefit", "economy"]


@dataclass(frozen=True, slots=True)
class EquipmentRecommendation:
    item_id: str
    brand: str
    line: str
    capacity_btu: int
    segment: EquipmentPreference
    features: tuple[str, ...]
    source_url: str
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
    """Return a conservative commercial reference, not an engineering load study.

    The assistant intentionally uses only the three inputs requested by the
    conversation flow. Conditions such as strong solar load, many electronics,
    unusual ceiling height or commercial use still need technical validation.
    """

    if area_m2 <= 0 or area_m2 > 500:
        raise ValueError("Area must be between 0 and 500 m²")
    if people <= 0 or people > 100:
        raise ValueError("People must be between 1 and 100")
    return max(9000, round((area_m2 * 600) + (max(0, people - 1) * 600)))


def recommend_equipment(
    area_m2: float,
    people: int,
    preference: EquipmentPreference,
) -> EquipmentRecommendation:
    required = required_capacity_btu(area_m2, people)
    catalog = equipment_catalog()

    # A capacity up to 10% below the simple rule is accepted as the same
    # commercial sizing band; otherwise the next available capacity is chosen.
    minimum_acceptable = required * 0.90
    candidates = [
        item
        for item in catalog
        if int(item["capacity_btu"]) >= minimum_acceptable
    ]
    if not candidates:
        candidates = list(catalog)

    preferred_capacity = min(int(item["capacity_btu"]) for item in candidates)
    same_capacity = [
        item
        for item in candidates
        if int(item["capacity_btu"]) == preferred_capacity
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
            segment_rank.get(str(item.get("segment")), 99),
            str(item.get("brand", "")),
            str(item.get("line", "")),
        ),
    )

    features = chosen.get("features")
    normalized_features = tuple(
        str(value)
        for value in features
        if isinstance(value, str) and value.strip()
    ) if isinstance(features, list) else ()

    segment = str(chosen.get("segment"))
    if segment not in {"modern", "cost_benefit", "economy"}:
        segment = "cost_benefit"

    return EquipmentRecommendation(
        item_id=str(chosen["id"]),
        brand=str(chosen["brand"]),
        line=str(chosen["line"]),
        capacity_btu=int(chosen["capacity_btu"]),
        segment=segment,  # type: ignore[arg-type]
        features=normalized_features,
        source_url=str(chosen.get("source_url") or ""),
        required_btu=required,
    )

from __future__ import annotations

from typing import Any


_RECOMMENDATION_INPUTS = frozenset(
    {
        "room_area_m2",
        "room_people_max",
        "equipment_preference",
        "equipment_cycle",
        "indoor_space_width_cm",
        "indoor_space_height_cm",
        "indoor_space_depth_cm",
        "indoor_space_unrestricted",
        "outdoor_space_width_cm",
        "outdoor_space_height_cm",
        "outdoor_space_depth_cm",
        "outdoor_space_unrestricted",
    }
)

_BUILDING_ONLY_FIELDS = frozenset(
    {
        "building_hours_start",
        "building_hours_end",
        "gate_instructions",
        "site_allowed_end",
        "site_limit_answered",
    }
)

_SCHEDULING_FIELDS = frozenset(
    {
        "selected_date",
        "selected_time",
        "candidate_booking",
    }
)

_PLANNING_INPUTS = frozenset(
    {
        "quantity",
        "access_condition",
        "service_address",
        "equipment_ownership",
        "equipment_model",
        "installation_height_over_3m",
        "work_at_height",
        "property_type",
        "building_hours_start",
        "building_hours_end",
        "gate_instructions",
        "tubing_meters",
        "site_allowed_end",
        "site_limit_answered",
    }
) | _RECOMMENDATION_INPUTS

_SERVICE_REUSABLE_FIELDS = frozenset(
    {
        "service_address",
        "property_type",
        "building_hours_start",
        "building_hours_end",
        "gate_instructions",
        "onsite_contact_mode",
        "onsite_contact_name",
        "whatsapp_contact_phone",
        "contact_phone",
        "contact_phone_confirmed",
    }
)


def invalidate_changed_facts(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Invalidate only derived facts affected by an explicit customer correction."""

    updated = dict(current)
    changed = {
        key
        for key in set(previous) | set(updated)
        if previous.get(key) != updated.get(key)
    }
    if changed & _RECOMMENDATION_INPUTS:
        updated.pop("recommended_equipment", None)
        updated.pop("recommendation_presented", None)
        updated.pop("quote_presented", None)

    if changed & _PLANNING_INPUTS:
        updated.pop("quote_presented", None)
        for field in _SCHEDULING_FIELDS:
            updated.pop(field, None)

    if "selected_date" in changed:
        updated.pop("selected_time", None)
        updated.pop("candidate_booking", None)

    if (
        "property_type" in changed
        and updated.get("property_type") == "house"
    ):
        for field in _BUILDING_ONLY_FIELDS:
            updated.pop(field, None)

    if changed & {"purchase_mode", "purchase_only", "request_mode"}:
        for field in (
            "delivery_method",
            "delivery_address",
            "address_purpose",
            "delivery_installation_match_pending",
            "quote_presented",
            *_SCHEDULING_FIELDS,
        ):
            updated.pop(field, None)
    return updated


def context_for_service_change(context: dict[str, Any]) -> dict[str, Any]:
    """Keep only facts that are safe to reuse for a different service."""

    return {
        key: value
        for key, value in context.items()
        if key in _SERVICE_REUSABLE_FIELDS
    }

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_SERVICE_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "default_service_catalog.json"
)
SUPPORTED_PRICING_TYPES = {"fixed", "estimated", "human_quote"}


class ServiceCatalogError(ValueError):
    """Raised when a versioned service template catalog is unsafe to consume."""


def load_default_service_catalog(
    path: Path = DEFAULT_SERVICE_CATALOG_PATH,
) -> dict[str, Any]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceCatalogError("Service catalog could not be loaded") from exc
    _validate_catalog(catalog)
    return catalog


def _validate_catalog(catalog: object) -> None:
    if not isinstance(catalog, dict):
        raise ServiceCatalogError("Service catalog must be an object")
    version = catalog.get("catalog_version")
    services = catalog.get("services")
    if not isinstance(version, str) or not version.strip():
        raise ServiceCatalogError("Service catalog version is required")
    if not isinstance(services, list) or not services:
        raise ServiceCatalogError("Service catalog templates are required")

    template_ids: set[str] = set()
    for template in services:
        if not isinstance(template, dict):
            raise ServiceCatalogError("Service template must be an object")
        template_id = template.get("template_id")
        if not isinstance(template_id, str) or not template_id.strip():
            raise ServiceCatalogError("Service template id is required")
        if template_id in template_ids:
            raise ServiceCatalogError("Service template ids must be unique")
        template_ids.add(template_id)

        pricing_type = template.get("pricing_type")
        if pricing_type not in SUPPORTED_PRICING_TYPES:
            raise ServiceCatalogError("Unsupported template pricing type")
        if not isinstance(template.get("requires_human_quote"), bool):
            raise ServiceCatalogError("Human quote policy must be explicit")
        if _contains_reference_values(template) and not _has_valid_sources(template):
            raise ServiceCatalogError(
                "Commercial reference values require documented sources"
            )


def _contains_reference_values(template: dict[str, Any]) -> bool:
    reference_price = template.get("reference_price")
    price_values = (
        reference_price.values() if isinstance(reference_price, dict) else ()
    )
    duration_values = (
        template.get("average_duration_minutes"),
        template.get("duration_reference_range_minutes"),
        template.get("additional_unit_duration_minutes"),
        template.get("difficult_access_duration_minutes"),
        template.get("duration_margin_minutes"),
    )
    return any(value is not None for value in (*price_values, *duration_values))


def _has_valid_sources(template: dict[str, Any]) -> bool:
    sources = template.get("sources")
    return isinstance(sources, list) and bool(sources) and all(
        isinstance(source, dict)
        and isinstance(source.get("title"), str)
        and bool(source["title"].strip())
        and isinstance(source.get("url"), str)
        and source["url"].startswith("https://")
        and isinstance(source.get("supports"), list)
        and bool(source["supports"])
        for source in sources
    )

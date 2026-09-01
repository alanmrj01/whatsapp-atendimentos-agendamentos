from pathlib import Path

from app.booking.catalog import (
    DEFAULT_SERVICE_CATALOG_PATH,
    load_default_service_catalog,
)


def test_default_catalog_is_versioned_and_has_unique_templates() -> None:
    catalog = load_default_service_catalog()
    template_ids = [service["template_id"] for service in catalog["services"]]

    assert catalog["catalog_version"] == "2026.09.01.1"
    assert catalog["reviewed_at"] == "2026-09-01"
    assert len(template_ids) == len(set(template_ids))


def test_all_commercial_references_have_dated_sources() -> None:
    catalog = load_default_service_catalog()

    for service in catalog["services"]:
        for source in service["sources"]:
            assert source["url"].startswith("https://")
            assert source["accessed_at"] == catalog["reviewed_at"]
            assert source["supports"]


def test_uncertain_services_are_human_quote_without_automatic_values() -> None:
    catalog = load_default_service_catalog()
    by_id = {service["template_id"]: service for service in catalog["services"]}

    for template_id in (
        "preventive_air_conditioning_maintenance_v1",
        "corrective_diagnosis_v1",
        "commercial_hvac_project_v1",
        "equipment_and_parts_v1",
    ):
        service = by_id[template_id]
        assert service["pricing_type"] == "human_quote"
        assert service["requires_human_quote"] is True
        assert service["average_duration_minutes"] is None


def test_simple_installation_defaults_are_traceable_and_conservative() -> None:
    catalog = load_default_service_catalog()
    service = next(
        item
        for item in catalog["services"]
        if item["template_id"] == "residential_split_simple_installation_v1"
    )

    duration_range = service["duration_reference_range_minutes"]
    assert duration_range["minimum"] <= service["average_duration_minutes"]
    assert service["average_duration_minutes"] <= duration_range["maximum"]
    assert (
        service["average_duration_minutes"] + service["duration_margin_minutes"]
        == duration_range["maximum"]
    )
    assert service["additional_unit_price"] is None
    assert service["difficult_access_price"] is None


def test_catalog_loader_does_not_share_or_persist_mutated_configuration() -> None:
    first_load = load_default_service_catalog()
    first_load["services"][0]["name"] = "Configuração da empresa"

    second_load = load_default_service_catalog()

    assert second_load["services"][0]["name"] != "Configuração da empresa"


def test_catalog_contains_no_environment_or_secret_file() -> None:
    assert DEFAULT_SERVICE_CATALOG_PATH == Path(
        "data/default_service_catalog.json"
    ).resolve()
    content = DEFAULT_SERVICE_CATALOG_PATH.read_text(encoding="utf-8").casefold()
    for forbidden in ("meta_access_token", "app_secret", "database_url", ".env"):
        assert forbidden not in content

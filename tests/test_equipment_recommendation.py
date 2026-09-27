from __future__ import annotations

from pytest import raises

from app.booking.equipment_recommender import (
    EquipmentCatalogEntry,
    equipment_catalog,
    recommend_equipment,
    required_capacity_btu,
)
from app.conversations.facts import enrich_context_from_message


def test_equipment_catalog_has_exactly_thirty_reference_configurations() -> None:
    items = equipment_catalog()

    assert len(items) == 30
    assert all(item["active"] is True for item in items)
    assert all(int(item["capacity_btu"]) > 0 for item in items)
    assert all(str(item["source_url"]).startswith("https://") for item in items)


def test_recommendation_uses_area_people_and_customer_preference() -> None:
    result = recommend_equipment(
        area_m2=15,
        people=2,
        preference="modern",
        climate_mode="heat_cool",
    )

    assert result.capacity_btu >= 9000
    assert result.segment == "modern"
    assert result.brand
    assert result.line


def test_required_capacity_grows_with_area_and_occupancy() -> None:
    small = required_capacity_btu(10, 1)
    larger = required_capacity_btu(20, 4)

    assert small == 9000
    assert larger > small


def test_fact_extractor_collects_future_answers_without_state_dependency() -> None:
    context = enrich_context_from_message(
        {},
        (
            "Quero uma cotação para instalação. Já tenho um LG Dual Inverter "
            "12000 BTU. É apartamento, das 08:00 às 17:00, e eu mesmo estarei no local."
        ),
        whatsapp_id="5512981359722",
    )

    assert context["request_mode"] == "quote"
    assert context["equipment_ownership"] == "has_equipment"
    assert "LG" in context["equipment_model"]
    assert context["property_type"] == "building"
    assert context["building_hours_start"] == "08:00"
    assert context["building_hours_end"] == "17:00"
    assert context["onsite_contact_mode"] == "customer"
    assert context["whatsapp_contact_phone"] == "+5512981359722"


def test_fact_extractor_collects_room_profile_in_fragments() -> None:
    context = enrich_context_from_message({}, "No máximo 4 pessoas")
    context = enrich_context_from_message(context, "O quarto tem uns 18 m2")
    context = enrich_context_from_message(context, "Quero bom custo-benefício")

    assert context["room_people_max"] == 4
    assert context["room_area_m2"] == 18
    assert context["equipment_preference"] == "cost_benefit"


def test_negative_model_phrase_is_not_mistaken_for_a_model() -> None:
    context = enrich_context_from_message({}, "Não tenho nenhum modelo em mente")

    assert "equipment_model" not in context

def test_cold_request_never_selects_explicit_heat_cool_model() -> None:
    entries = (
        EquipmentCatalogEntry(
            item_id="tcl-qf",
            brand="TCL",
            line="A2 Inverter Quente/Frio",
            capacity_btu=12000,
            segment="economy",
            cycles=("cold", "heat_cool"),
        ),
        EquipmentCatalogEntry(
            item_id="gree-family",
            brand="Gree",
            line="G-Top Auto Inverter",
            capacity_btu=12000,
            segment="economy",
            cycles=("cold",),
        ),
    )

    result = recommend_equipment(
        area_m2=16,
        people=4,
        preference="economy",
        climate_mode="cold",
        entries=entries,
    )

    assert result.item_id == "gree-family"
    assert result.selected_cycle == "cold"
    assert "Só Frio" in result.label
    assert "Quente/Frio" not in result.label


def test_cold_request_fails_closed_when_only_explicit_heat_cool_model_exists() -> None:
    entries = (
        EquipmentCatalogEntry(
            item_id="tcl-qf",
            brand="TCL",
            line="A2 Inverter Quente/Frio",
            capacity_btu=12000,
            segment="economy",
            cycles=("cold", "heat_cool"),
        ),
    )

    with raises(ValueError, match="requested cycle"):
        recommend_equipment(
            area_m2=16,
            people=4,
            preference="economy",
            climate_mode="cold",
            entries=entries,
        )


def test_heat_cool_request_keeps_explicit_heat_cool_model() -> None:
    entry = EquipmentCatalogEntry(
        item_id="tcl-qf",
        brand="TCL",
        line="A2 Inverter Quente/Frio",
        capacity_btu=12000,
        segment="economy",
        cycles=("cold", "heat_cool"),
    )

    result = recommend_equipment(
        area_m2=16,
        people=4,
        preference="economy",
        climate_mode="heat_cool",
        entries=(entry,),
    )

    assert result.item_id == "tcl-qf"
    assert result.selected_cycle == "heat_cool"
    assert result.label.count("Quente/Frio") == 1

def test_recommendation_respects_internal_and_external_space_limits() -> None:
    too_large = EquipmentCatalogEntry(
        item_id="too-large",
        brand="Marca A",
        line="Linha A",
        capacity_btu=12000,
        segment="cost_benefit",
        cycles=("cold",),
        indoor_dimensions_cm={"width": 95.0, "height": 32.0, "depth": 25.0},
        outdoor_dimensions_cm={"width": 80.0, "height": 60.0, "depth": 35.0},
    )
    fits = EquipmentCatalogEntry(
        item_id="fits",
        brand="Marca B",
        line="Linha B",
        capacity_btu=12000,
        segment="cost_benefit",
        cycles=("cold",),
        indoor_dimensions_cm={"width": 78.0, "height": 28.0, "depth": 20.0},
        outdoor_dimensions_cm={"width": 60.0, "height": 50.0, "depth": 30.0},
    )

    result = recommend_equipment(
        area_m2=16,
        people=4,
        preference="cost_benefit",
        climate_mode="cold",
        indoor_space=(80.0, 30.0, 22.0),
        outdoor_space=(65.0, 55.0, 32.0),
        entries=(too_large, fits),
    )

    assert result.item_id == "fits"


def test_recommendation_fails_closed_when_dimensions_are_unknown_under_restriction() -> None:
    unknown = EquipmentCatalogEntry(
        item_id="unknown-size",
        brand="Marca",
        line="Linha",
        capacity_btu=12000,
        segment="cost_benefit",
        cycles=("cold",),
    )

    with raises(ValueError, match="installation space"):
        recommend_equipment(
            area_m2=16,
            people=4,
            preference="cost_benefit",
            climate_mode="cold",
            indoor_space=(80.0, 30.0, 22.0),
            entries=(unknown,),
        )


from __future__ import annotations

from pytest import raises

from app.booking.equipment_recommender import (
    EquipmentCatalogEntry,
    equipment_catalog,
    recommend_equipment,
    required_capacity_btu,
)
from app.conversations.facts import enrich_context_from_message
from app.conversations.context_policy import invalidate_changed_facts


def test_equipment_catalog_has_exactly_thirty_reference_configurations() -> None:
    items = equipment_catalog()

    assert len(items) == 30
    assert len({str(item["id"]) for item in items}) == 30
    assert all(item["active"] is True for item in items)
    assert all(int(item["capacity_btu"]) > 0 for item in items)
    assert all(str(item["source_url"]).startswith("https://") for item in items)
    assert all(str(item["image_url"]).startswith("https://") for item in items)
    assert all(float(item["reference_price_brl"]) > 0 for item in items)
    assert all(item["price_reviewed_at"] == "2026-09-28" for item in items)
    assert all(
        isinstance(item.get("price_sources"), list)
        and item["price_sources"]
        and all(str(url).startswith("https://") for url in item["price_sources"])
        for item in items
    )

    brands = {str(item["brand"]) for item in items}
    assert {
        "Samsung", "LG", "Midea", "Gree", "Electrolux", "Philco",
        "TCL", "Agratto", "Daikin", "Elgin", "Hisense",
    } <= brands

    cycles = [tuple(item["cycles"]) for item in items]
    assert ("cold",) in cycles
    assert ("heat_cool",) in cycles
    assert all(cycle in {("cold",), ("heat_cool",)} for cycle in cycles)

    capacities = {int(item["capacity_btu"]) for item in items}
    assert {9000, 12000, 18000, 24000} <= capacities

    dimensioned = [
        item
        for item in items
        if item.get("indoor_dimensions_cm")
        and item.get("outdoor_dimensions_cm")
    ]
    assert len(dimensioned) >= 20


def test_default_catalog_can_recommend_both_commercial_cycles() -> None:
    cold = recommend_equipment(
        area_m2=16,
        people=2,
        preference="cost_benefit",
        climate_mode="cold",
    )
    heat_cool = recommend_equipment(
        area_m2=16,
        people=2,
        preference="cost_benefit",
        climate_mode="heat_cool",
    )

    assert cold.cycles == ("cold",)
    assert cold.selected_cycle == "cold"
    assert "Só Frio" in cold.label
    assert heat_cool.cycles == ("heat_cool",)
    assert heat_cool.selected_cycle == "heat_cool"
    assert "Quente/Frio" in heat_cool.label


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


def test_latest_profile_corrections_win_and_invalidate_recommendation() -> None:
    context = {
        "room_people_max": 4,
        "room_area_m2": 20,
        "equipment_cycle": "heat_cool",
        "recommended_equipment": {"item_id": "old"},
        "recommendation_presented": True,
    }

    context = enrich_context_from_message(
        context,
        "Eram 4 pessoas, na verdade são 2",
    )
    context = enrich_context_from_message(
        context,
        "Eram 20 m², corrigindo, 15",
    )
    context = enrich_context_from_message(
        context,
        "Não quero quente/frio, quero só frio",
    )

    assert context["room_people_max"] == 2
    assert context["room_area_m2"] == 15
    assert context["equipment_cycle"] == "cold"
    assert "recommended_equipment" not in context
    assert "recommendation_presented" not in context


def test_cycle_correction_can_switch_back_to_heat_cool() -> None:
    context = enrich_context_from_message(
        {"equipment_cycle": "cold"},
        "Não quero só frio, quero quente/frio",
    )

    assert context["equipment_cycle"] == "heat_cool"


def test_house_correction_removes_building_only_context() -> None:
    context = enrich_context_from_message(
        {
            "property_type": "building",
            "building_hours_start": "08:00",
            "building_hours_end": "17:00",
            "gate_instructions": "Interfone 2",
        },
        "É casa, não apartamento",
    )

    assert context["property_type"] == "house"
    assert "building_hours_start" not in context
    assert "building_hours_end" not in context
    assert "gate_instructions" not in context


def test_new_date_invalidates_selected_time_only() -> None:
    updated = invalidate_changed_facts(
        {
            "service_id": "service-1",
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"id": "candidate"},
        },
        {
            "service_id": "service-1",
            "selected_date": "2026-09-03",
            "selected_time": "09:00",
            "candidate_booking": {"id": "candidate"},
        },
    )

    assert updated["service_id"] == "service-1"
    assert updated["selected_date"] == "2026-09-03"
    assert "selected_time" not in updated
    assert "candidate_booking" not in updated


def test_address_with_block_and_apartment_does_not_become_gate_instruction() -> None:
    context = enrich_context_from_message(
        {"property_type": "building"},
        "Rua X, 100, bloco 2, apto 31",
    )

    assert "gate_instructions" not in context



def test_changed_planning_fact_invalidates_quote_and_schedule() -> None:
    updated = invalidate_changed_facts(
        {
            "quantity": 1,
            "quote_presented": True,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"id": "candidate"},
        },
        {
            "quantity": 2,
            "quote_presented": True,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"id": "candidate"},
        },
    )

    assert updated["quantity"] == 2
    assert "quote_presented" not in updated
    assert "selected_date" not in updated
    assert "selected_time" not in updated
    assert "candidate_booking" not in updated


def test_recommendation_respects_customer_equipment_budget() -> None:
    affordable = EquipmentCatalogEntry(
        item_id="affordable",
        brand="Marca A",
        line="Linha Econômica",
        capacity_btu=12000,
        segment="economy",
        cycles=("cold",),
        price=1900.0,
    )
    expensive = EquipmentCatalogEntry(
        item_id="expensive",
        brand="Marca B",
        line="Linha Premium",
        capacity_btu=12000,
        segment="modern",
        cycles=("cold",),
        price=2600.0,
    )

    result = recommend_equipment(
        area_m2=16,
        people=3,
        preference="modern",
        climate_mode="cold",
        entries=(expensive, affordable),
        budget_max=2000.0,
    )

    assert result.item_id == "affordable"
    assert result.within_budget is True


def test_recommendation_returns_nearest_compatible_option_when_budget_is_too_low() -> None:
    cheaper = EquipmentCatalogEntry(
        item_id="cheaper",
        brand="Marca A",
        line="Linha A",
        capacity_btu=12000,
        segment="cost_benefit",
        cycles=("cold",),
        price=2100.0,
    )
    expensive = EquipmentCatalogEntry(
        item_id="expensive",
        brand="Marca B",
        line="Linha B",
        capacity_btu=12000,
        segment="economy",
        cycles=("cold",),
        price=2400.0,
    )

    result = recommend_equipment(
        area_m2=16,
        people=3,
        preference="economy",
        climate_mode="cold",
        entries=(expensive, cheaper),
        budget_max=1500.0,
    )

    assert result.item_id == "cheaper"
    assert result.within_budget is False


def test_budget_change_invalidates_existing_equipment_recommendation() -> None:
    updated = invalidate_changed_facts(
        {
            "equipment_budget_max": 2500.0,
            "recommended_equipment": {"item_id": "old"},
            "recommendation_presented": True,
            "quote_presented": True,
        },
        {
            "equipment_budget_max": 1800.0,
            "recommended_equipment": {"item_id": "old"},
            "recommendation_presented": True,
            "quote_presented": True,
        },
    )

    assert updated["equipment_budget_max"] == 1800.0
    assert "recommended_equipment" not in updated
    assert "recommendation_presented" not in updated
    assert "quote_presented" not in updated

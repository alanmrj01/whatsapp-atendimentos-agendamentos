from __future__ import annotations

from app.booking.equipment_recommender import (
    equipment_catalog,
    recommend_equipment,
    required_capacity_btu,
)
from app.conversations.facts import enrich_context_from_message
from app.conversations.outbound import OutboundMessage, split_outbound_message


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
    )

    assert result.capacity_btu >= 9000
    assert result.segment == "modern"
    assert result.brand
    assert result.line
    assert result.image_url is not None


def test_recommendation_respects_cycle_and_business_catalog() -> None:
    result = recommend_equipment(
        area_m2=15,
        people=2,
        preference="economy",
        cycle="heat_cool",
        allowed_item_ids={"tcl-a2-inverter-quente-frio-9000"},
    )

    assert result.item_id == "tcl-a2-inverter-quente-frio-9000"
    assert result.cycle == "heat_cool"


def test_recommendation_rejects_empty_business_catalog() -> None:
    try:
        recommend_equipment(
            area_m2=15,
            people=2,
            preference="modern",
            allowed_item_ids=set(),
        )
    except ValueError as exc:
        assert "No active equipment" in str(exc)
    else:
        raise AssertionError("empty company catalog must fail closed")


def test_disabled_model_cannot_be_selected_from_business_catalog() -> None:
    result = recommend_equipment(
        area_m2=15,
        people=2,
        preference="modern",
        allowed_item_ids={"gree-g-top-auto-inverter-9000"},
    )

    assert result.item_id == "gree-g-top-auto-inverter-9000"
    assert result.item_id != "lg-ai-dual-inverter-voice-9000"


def test_qualitative_or_measured_space_restrictions_fail_closed() -> None:
    for status in ("limited", "technical_balcony", "measured"):
        try:
            recommend_equipment(
                area_m2=15,
                people=2,
                preference="economy",
                outdoor_space_status=status,
            )
        except ValueError as exc:
            assert "Physical installation constraints" in str(exc)
        else:
            raise AssertionError("unverified physical fit must fail closed")


def test_unknown_condenser_shape_is_not_invented() -> None:
    try:
        recommend_equipment(
            area_m2=15,
            people=2,
            preference="economy",
            condenser_type_preference="compact",
        )
    except ValueError as exc:
        assert "condenser constraint" in str(exc)
    else:
        raise AssertionError("catalog must not invent condenser compatibility")


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
    context = enrich_context_from_message(context, "Quero quente e frio")

    assert context["room_people_max"] == 4
    assert context["room_area_m2"] == 18
    assert context["equipment_preference"] == "cost_benefit"
    assert context["equipment_cycle"] == "heat_cool"


def test_fact_extractor_collects_complete_profile_without_crossing_space_facts() -> None:
    context = enrich_context_from_message(
        {},
        (
            "Quero quente e frio para um quarto de 18 m2 com 4 pessoas, "
            "com bom custo-benefício. A evaporadora tem bastante espaço; "
            "a condensadora fica em varanda técnica pequena e precisa ser compacta."
        ),
    )

    assert context["room_area_m2"] == 18
    assert context["room_people_max"] == 4
    assert context["equipment_preference"] == "cost_benefit"
    assert context["equipment_cycle"] == "heat_cool"
    assert context["indoor_space_status"] == "ample"
    assert context["outdoor_space_status"] == "technical_balcony"
    assert context["condenser_type_preference"] == "compact"


def test_exact_space_dimensions_are_accepted_without_claiming_fit() -> None:
    context = enrich_context_from_message(
        {},
        "A evaporadora tem um espaço interno de 80 x 30 cm.",
    )

    assert context["indoor_space_status"] == "measured"
    assert "80 x 30 cm" in context["indoor_space_details"]


def test_latest_explicit_profile_correction_wins() -> None:
    context = enrich_context_from_message(
        {},
        "São 2 pessoas, 10 m2 e somente frio.",
    )
    corrected = enrich_context_from_message(
        context,
        "Corrigindo: são 5 pessoas, 22 m2 e quero quente e frio.",
    )

    assert corrected["room_people_max"] == 5
    assert corrected["room_area_m2"] == 22
    assert corrected["equipment_cycle"] == "heat_cool"


def test_explicit_city_correction_replaces_address_and_invalidates_slot() -> None:
    context = {
        "service_address": {
            "address_line": "Rua A, 10",
            "city": "São José dos Campos",
            "state": "SP",
        },
        "selected_date": "2026-09-30",
        "selected_time": "09:00",
        "candidate_booking": {"employee_id": "test"},
    }

    corrected = enrich_context_from_message(context, "Desculpa, é Jacareí.")

    assert corrected["service_address"]["city"] == "Jacareí"
    assert "selected_date" not in corrected
    assert "selected_time" not in corrected
    assert "candidate_booking" not in corrected


def test_outbound_policy_splits_more_than_four_logical_lines() -> None:
    message = OutboundMessage(
        message_type="interactive_button",
        body="1\n2\n3\n4\n5\n6",
        interactive_id="test.buttons",
        outbound_payload={"buttons": []},
    )

    parts = split_outbound_message(message)

    assert [part.body for part in parts] == ["1\n2\n3\n4", "5\n6"]
    assert parts[0].message_type == "text"
    assert parts[1].message_type == "interactive_button"


def test_negative_model_phrase_is_not_mistaken_for_a_model() -> None:
    context = enrich_context_from_message({}, "Não tenho nenhum modelo em mente")

    assert "equipment_model" not in context

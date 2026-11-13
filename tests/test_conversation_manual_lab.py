from decimal import Decimal

import pytest

from app.conversation_lab.adapters import (
    DIAGNOSTICS_SERVICE_ID,
    ELECTRICAL_SERVICE_ID,
    INSTALLATION_SERVICE_ID,
    REFRIGERATOR_SERVICE_ID,
    SERVICE_ID,
)
from app.conversation_lab.manual import (
    HTML,
    ManualInput,
    _SESSIONS,
    create_session,
    health,
    send_message,
)
from app.conversations.constants import (
    EQUIPMENT_BOTH,
    QUOTE_SCHEDULE,
    ConversationState,
)
from app.conversations.facts import enrich_context_from_message
from app.conversations.outbound import (
    tubing_guidance_message,
    tubing_variation_message,
)


async def _new_session():
    _SESSIONS.clear()
    snapshot = await create_session()
    session_id = snapshot["session_id"]
    return session_id, _SESSIONS[session_id]


async def _new_named_session():
    session_id, session = await _new_session()
    session.repository.customer_name = "João"
    return session_id, session


@pytest.mark.asyncio
async def test_manual_lab_health_and_isolation_flags() -> None:
    assert await health() == {
        "status": "ok",
        "mode": "manual-conversation-lab",
    }

    session_id, _ = await _new_session()
    snapshot = await create_session()
    assert snapshot["session_id"] != session_id
    assert snapshot["lab"] == {
        "external_io": False,
        "database": False,
        "meta_whatsapp": False,
        "billing": False,
        "tracking": False,
        "booking_actions": "dry-run",
    }


@pytest.mark.asyncio
async def test_manual_lab_starts_without_synthetic_customer_name_and_asks_real_name() -> None:
    session_id, _ = await _new_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="boa tarde"),
    )

    assert snapshot["state"] == ConversationState.CUSTOMER_NAME.value
    assert snapshot["customer_name"] is None
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "como posso te chamar" in joined
    assert "cliente sintético" not in joined


@pytest.mark.asyncio
async def test_manual_lab_has_fixed_isolated_equipment_catalog() -> None:
    _, session = await _new_session()

    catalog = await session.port.list_equipment_catalog(None)

    assert len(catalog) >= 3
    assert {item.capacity_btu for item in catalog} >= {9000, 12000, 18000}
    assert all(item.source_url.startswith("lab://") for item in catalog)


def test_manual_lab_exposes_copy_conversation_button() -> None:
    assert "Copiar conversa" in HTML
    assert "copyConversation()" in HTML


@pytest.mark.parametrize(
    ("body", "expected_service_id"),
    [
        ("minha geladeira não está gelando", REFRIGERATOR_SERVICE_ID),
        ("meu freezer não congela", REFRIGERATOR_SERVICE_ID),
        ("a tomada da cozinha queimou", ELECTRICAL_SERVICE_ID),
        (
            "meu ar condicionado não está gelando e preciso de manutenção",
            DIAGNOSTICS_SERVICE_ID,
        ),
    ],
)
@pytest.mark.asyncio
async def test_manual_lab_uses_r9_catalog_driven_service_selection(
    body: str,
    expected_service_id,
) -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text=body),
    )

    assert snapshot["state"] == ConversationState.BOOKING_ADDRESS.value
    assert snapshot["context"]["service_id"] == str(expected_service_id)
    assert "services" in snapshot["dry_run_calls"]
    assert any(item["role"] == "assistant" for item in snapshot["history"])


@pytest.mark.parametrize(
    ("body", "removed_service_id"),
    [
        ("minha geladeira não está gelando", REFRIGERATOR_SERVICE_ID),
        ("o chuveiro não liga", ELECTRICAL_SERVICE_ID),
    ],
)
@pytest.mark.asyncio
async def test_manual_lab_does_not_invent_service_outside_catalog(
    body: str,
    removed_service_id,
) -> None:
    session_id, session = await _new_named_session()
    session.port.services = [
        option
        for option in session.port.services
        if option.id != str(removed_service_id)
    ]
    session.port.service_intakes.pop(removed_service_id, None)

    snapshot = await send_message(
        session_id,
        ManualInput(text=body),
    )

    assert snapshot["context"].get("service_id") is None
    assert snapshot["state"] != ConversationState.BOOKING_ADDRESS.value


@pytest.mark.asyncio
async def test_manual_lab_commercial_negotiation_hands_off_without_losing_candidate() -> None:
    session_id, session = await _new_named_session()
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-11-16",
        "selected_time": "09:00",
    }
    session.repository.state = ConversationState.BOOKING_CONFIRM
    session.repository.context = {
        **candidate,
        "candidate_booking": dict(candidate),
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="faz um preço melhor?"),
    )

    assert snapshot["state"] == ConversationState.HUMAN_HANDOFF.value
    assert snapshot["automation_enabled"] is False
    assert snapshot["handoff_status"] == "waiting"
    assert snapshot["context"]["candidate_booking"] == candidate


@pytest.mark.asyncio
async def test_manual_lab_synthetic_audio_is_processed_without_external_io() -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(message_type="audio"),
    )

    assert snapshot["lab"]["external_io"] is False
    assert snapshot["lab"]["meta_whatsapp"] is False
    assert any(item["role"] == "assistant" for item in snapshot["history"])


@pytest.mark.asyncio
async def test_purchase_intent_only_asks_whether_installation_is_also_wanted() -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="quero comprar um ar condicionado"),
    )

    option_titles = [
        option["title"]
        for item in snapshot["history"]
        if item["role"] == "assistant"
        for option in item.get("options", [])
    ]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()

    assert snapshot["state"] == ConversationState.BOOKING_SERVICE.value
    assert "você também gostaria de contratar a instalação" in joined
    assert option_titles == ["Só o aparelho", "Aparelho + instalação"]


@pytest.mark.asyncio
async def test_purchase_plus_installation_preserves_short_term_facts_and_large_quantity() -> None:
    session_id, _ = await _new_named_session()

    await send_message(
        session_id,
        ManualInput(text="quero comprar um ar condicionado"),
    )
    snapshot = await send_message(
        session_id,
        ManualInput(
            text="aparelho + instalação",
            action=EQUIPMENT_BOTH,
        ),
    )

    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value
    assert snapshot["context"]["purchase_mode"] == "both"
    assert snapshot["context"]["equipment_ownership"] == "needs_equipment"
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert "Quantos aparelhos de ar-condicionado são?" in last["body"]
    assert [option["title"] for option in last["options"]][-1] == "6 ou mais"

    snapshot = await send_message(
        session_id,
        ManualInput(text="6 ou mais", action="quantity:more"),
    )
    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value
    assert "ao todo" in [
        item for item in snapshot["history"] if item["role"] == "assistant"
    ][-1]["body"].casefold()

    snapshot = await send_message(
        session_id,
        ManualInput(text="12"),
    )
    assert snapshot["context"]["quantity"] == 12
    assert snapshot["context"]["purchase_mode"] == "both"
    assert snapshot["context"]["equipment_ownership"] == "needs_equipment"
    assert snapshot["state"] != ConversationState.BOOKING_ADDRESS.value
    assert snapshot["state"] != ConversationState.BOOKING_EQUIPMENT_OWNERSHIP.value


@pytest.mark.asyncio
async def test_used_equipment_request_hands_off_instead_of_inventing_catalog_option() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_PROFILE
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "purchase_mode": "both",
        "purchase_only": False,
        "equipment_ownership": "needs_equipment",
        "quantity": 1,
        "room_people_max": 3,
        "room_area_m2": 16,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="quero mais barato, se possível usado"),
    )

    assert snapshot["state"] == ConversationState.HUMAN_HANDOFF.value
    assert snapshot["automation_enabled"] is False
    assert snapshot["handoff_status"] == "waiting"
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "fora do catálogo ativo" in joined
    assert "usado" in joined


@pytest.mark.asyncio
async def test_customer_can_ask_what_pending_question_means_without_repair_penalty() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_PROFILE
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "purchase_mode": "both",
        "purchase_only": False,
        "equipment_ownership": "needs_equipment",
        "quantity": 1,
        "room_people_max": 3,
        "room_area_m2": 16,
        "equipment_preference": "economy",
        "equipment_cycle": "cold",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="não entendi, como assim parte interna ou externa?"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    assert "repair_attempts" not in snapshot["context"]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "unidade interna" in joined
    assert "condensadora" in joined
    assert "sem restrição" in joined


@pytest.mark.asyncio
async def test_lateral_ladder_fact_is_remembered_and_height_is_not_reasked() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_PROFILE
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "purchase_mode": "both",
        "purchase_only": False,
        "equipment_ownership": "needs_equipment",
        "quantity": 1,
        "room_people_max": 3,
        "room_area_m2": 16,
        "equipment_preference": "economy",
        "equipment_cycle": "cold",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="vai precisar de uma escada de 4 mts"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    assert snapshot["context"]["installation_height_over_3m"] is True
    assert snapshot["context"]["work_at_height"] is True
    assert "repair_attempts" not in snapshot["context"]

    snapshot = await send_message(
        session_id,
        ManualInput(text="sem restrição"),
    )

    assert snapshot["context"]["installation_height_over_3m"] is True
    assert snapshot["state"] != ConversationState.BOOKING_INSTALLATION_HEIGHT.value


@pytest.mark.asyncio
async def test_quote_precedes_building_rules_and_schedule_decision_resumes_them() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_TUBING
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "quantity": 1,
        "equipment_ownership": "has_equipment",
        "equipment_model": "Split 12.000 BTU",
        "service_address": {
            "address_line": "Avenida Rui Barbosa, 688 apto 101",
            "city": "São José dos Campos",
            "state": "SP",
        },
        "installation_height_over_3m": True,
        "work_at_height": True,
        "tube_disclaimer_sent": True,
        "property_type": "building",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="5 mts"),
    )

    assert snapshot["state"] == ConversationState.QUOTE_DECISION.value
    assert "building_hours_start" not in snapshot["context"]
    assert "gate_instructions" not in snapshot["context"]

    snapshot = await send_message(
        session_id,
        ManualInput(text="consultar agenda", action=QUOTE_SCHEDULE),
    )

    assert snapshot["state"] == ConversationState.BOOKING_BUILDING_HOURS.value


@pytest.mark.asyncio
async def test_gate_request_for_technician_name_continues_scheduling_professionally() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_GATE_DETAILS
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "quote_presented": True,
        "quantity": 1,
        "equipment_ownership": "has_equipment",
        "equipment_model": "Split 12.000 BTU",
        "service_address": {
            "address_line": "Avenida Rui Barbosa, 688 apto 101",
            "city": "São José dos Campos",
            "state": "SP",
        },
        "installation_height_over_3m": True,
        "work_at_height": True,
        "tube_disclaimer_sent": True,
        "tubing_length_answered": True,
        "tubing_meters": "5",
        "property_type": "building",
        "building_hours_start": "08:00",
        "building_hours_end": "17:00",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="preciso do nome do instalador para liberar na portaria"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_DATE.value
    assert "nome do técnico responsável" in snapshot["context"]["gate_instructions"]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "depois de definirmos a data e o horário" in joined
    assert "confirma qual técnico" in joined


def test_apartment_address_fact_infers_building_for_short_term_memory() -> None:
    context = enrich_context_from_message(
        {},
        "avenida rui barbosa, 688 apto 101",
    )

    assert context["property_type"] == "building"


def test_tubing_guidance_defers_exact_extra_price_until_quote() -> None:
    guidance = tubing_guidance_message(
        included_meters=Decimal("3"),
        extra_meter_price=Decimal("50"),
        unit_label="metro",
    )
    variation = tubing_variation_message(
        included_meters=Decimal("3"),
        extra_meter_price=Decimal("50"),
        unit_label="metro",
    )

    assert "R$" not in guidance.body
    assert "R$" not in variation.body
    assert "adicional no orçamento" in guidance.body
    assert "adicional no orçamento" in variation.body

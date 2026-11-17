from decimal import Decimal

import pytest

from app.conversation_lab.adapters import (
    DEINSTALLATION_SERVICE_ID,
    DIAGNOSTICS_SERVICE_ID,
    ELECTRICAL_SERVICE_ID,
    GENERIC_SERVICE_ID,
    INFRASTRUCTURE_SERVICE_ID,
    INSTALLATION_SERVICE_ID,
    PMOC_SERVICE_ID,
    REFRIGERATOR_SERVICE_ID,
    RELOCATION_SERVICE_ID,
    SERVICE_ID,
)
from app.conversation_lab.manual import (
    HTML,
    ManualInput,
    _SESSIONS,
    create_session,
    health,
    send_message,
    _send_transcribed_audio,
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
        "speech_to_text": False,
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
async def test_manual_lab_short_greeting_then_name_does_not_emit_unknown_fallback() -> None:
    session_id, _ = await _new_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="opa"),
    )
    assert snapshot["state"] == ConversationState.CUSTOMER_NAME.value

    snapshot = await send_message(
        session_id,
        ManualInput(text="alan"),
    )

    assert snapshot["state"] == ConversationState.MENU.value
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert last["body"] == "Prazer, Alan. Como posso te ajudar?"
    assert "não entendi" not in last["body"].casefold()
    assert "nao entendi" not in last["body"].casefold()


@pytest.mark.asyncio
async def test_manual_lab_service_opener_asks_name_without_unnecessary_claro() -> None:
    session_id, _ = await _new_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="opa preciso limpar um ar"),
    )

    assert snapshot["state"] == ConversationState.CUSTOMER_NAME.value
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert "como posso te chamar" in last["body"].casefold()
    assert "claro" not in last["body"].casefold()


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


def test_manual_lab_exposes_real_audio_upload() -> None:
    assert "Enviar arquivo de áudio" in HTML
    assert 'id="audioFile"' in HTML
    assert "sendAudioFile" in HTML


def test_manual_lab_exposes_in_browser_audio_recording() -> None:
    assert "Gravar áudio" in HTML
    assert 'id="recordAudioButton"' in HTML
    assert "toggleAudioRecording()" in HTML
    assert "navigator.mediaDevices.getUserMedia({audio:true})" in HTML
    assert "new MediaRecorder" in HTML
    assert "audio/webm;codecs=opus" in HTML
    assert "Parar e enviar" in HTML


@pytest.mark.parametrize(
    ("body", "expected_service_id", "expected_state"),
    [
        (
            "minha geladeira não está gelando",
            REFRIGERATOR_SERVICE_ID,
            ConversationState.BOOKING_EQUIPMENT_MODEL,
        ),
        (
            "meu freezer não congela",
            REFRIGERATOR_SERVICE_ID,
            ConversationState.BOOKING_EQUIPMENT_MODEL,
        ),
        (
            "a tomada da cozinha queimou",
            ELECTRICAL_SERVICE_ID,
            ConversationState.BOOKING_PROPERTY,
        ),
        (
            "meu ar condicionado não está gelando e preciso de manutenção",
            DIAGNOSTICS_SERVICE_ID,
            ConversationState.BOOKING_EQUIPMENT_MODEL,
        ),
    ],
)
@pytest.mark.asyncio
async def test_manual_lab_uses_r9_catalog_driven_service_selection(
    body: str,
    expected_service_id,
    expected_state: ConversationState,
) -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text=body),
    )

    assert snapshot["state"] == expected_state.value
    assert snapshot["context"]["service_id"] == str(expected_service_id)
    assert "services" in snapshot["dry_run_calls"]
    assert any(item["role"] == "assistant" for item in snapshot["history"])
    assert "service_address" not in snapshot["context"]


@pytest.mark.asyncio
async def test_manual_lab_smell_complaint_routes_to_cleaning_after_name_capture() -> None:
    session_id, _ = await _new_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="blz? meu ar ta fedendo"),
    )

    assert snapshot["state"] == ConversationState.CUSTOMER_NAME.value

    snapshot = await send_message(
        session_id,
        ManualInput(text="joao"),
    )

    assert snapshot["context"]["service_id"] == str(SERVICE_ID)
    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "quantos itens ou aparelhos" in joined


@pytest.mark.asyncio
async def test_manual_lab_refrigerator_qualifies_appliance_before_address() -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="minha geladeira não está gelando"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assert snapshot["context"]["appliance_type"] == "refrigerator"
    assert snapshot["context"]["service_issue_category"] == "no_cooling"
    assert "service_address" not in snapshot["context"]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "marca" in joined
    assert "modelo" in joined
    assert "endereço" not in joined

    snapshot = await send_message(
        session_id,
        ManualInput(text="Brastemp BRM44"),
    )

    assert snapshot["state"] != ConversationState.BOOKING_ADDRESS.value
    assert snapshot["context"]["equipment_model"] == "Brastemp BRM44"
    assert "service_address" not in snapshot["context"]
    assert "endereço" not in snapshot["history"][-1]["body"].casefold()


@pytest.mark.asyncio
async def test_manual_lab_electrical_qualifies_risk_and_property_before_address() -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="a tomada da cozinha queimou"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_PROPERTY.value
    assert snapshot["context"]["service_issue_category"] == "outlet"
    assert "service_address" not in snapshot["context"]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "risco elétrico" in joined
    assert "disjuntor" in joined
    assert "casa" in joined

    snapshot = await send_message(
        session_id,
        ManualInput(text="é uma casa"),
    )

    assert snapshot["state"] != ConversationState.BOOKING_ADDRESS.value
    assert snapshot["context"]["property_type"] == "house"
    assert "service_address" not in snapshot["context"]
    assert "endereço" not in snapshot["history"][-1]["body"].casefold()


@pytest.mark.parametrize(
    ("service_id", "label", "details"),
    [
        (
            PMOC_SERVICE_ID,
            "PMOC - Plano de manutenção, operação e controle",
            "preciso fazer o PMOC em 4 aparelhos da empresa",
        ),
        (
            DEINSTALLATION_SERVICE_ID,
            "Desinstalação de ar-condicionado",
            "preciso desinstalar 2 aparelhos em uma empresa",
        ),
        (
            RELOCATION_SERVICE_ID,
            "Realocação de ar-condicionado",
            "preciso realocar 2 aparelhos entre dois ambientes",
        ),
        (
            INFRASTRUCTURE_SERVICE_ID,
            "Infraestrutura para ar-condicionado",
            "preciso preparar infraestrutura para 3 ambientes da empresa",
        ),
        (
            GENERIC_SERVICE_ID,
            "Atendimento técnico especial",
            "preciso verificar um equipamento que parou durante o uso",
        ),
    ],
)
@pytest.mark.asyncio
async def test_manual_lab_motor_wide_services_qualify_scope_before_address(
    service_id,
    label: str,
    details: str,
) -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_SERVICE

    snapshot = await send_message(
        session_id,
        ManualInput(
            text=label,
            action=f"service:{service_id}",
        ),
    )

    assert snapshot["state"] == ConversationState.BOOKING_SERVICE_DETAILS.value
    assert snapshot["context"]["service_id"] == str(service_id)
    assert "service_address" not in snapshot["context"]
    assert "antes de pedir o endereço" in snapshot["history"][-1]["body"].casefold()

    snapshot = await send_message(
        session_id,
        ManualInput(text=details),
    )

    assert snapshot["state"] != ConversationState.BOOKING_ADDRESS.value
    assert snapshot["context"]["service_details_text"]
    assert "service_address" not in snapshot["context"]
    assert "endereço" not in snapshot["history"][-1]["body"].casefold()


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
async def test_manual_lab_real_audio_is_transcribed_before_engine(
    monkeypatch,
) -> None:
    from app.conversation_lab import manual as manual_lab

    session_id, _ = await _new_named_session()

    async def fake_transcribe(_: bytes) -> str:
        return "quero fazer uma limpeza no ar condicionado"

    monkeypatch.setattr(manual_lab, "SPEECH_ENABLED", True)
    monkeypatch.setattr(manual_lab, "transcribe_short_audio", fake_transcribe)

    snapshot = await _send_transcribed_audio(
        session_id,
        b"audio-fixture",
        "audio/ogg",
    )

    user_messages = [
        item for item in snapshot["history"] if item["role"] == "user"
    ]
    assert user_messages[-1]["message_type"] == "audio"
    assert (
        user_messages[-1]["transcription"]
        == "quero fazer uma limpeza no ar condicionado"
    )
    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value


@pytest.mark.asyncio
async def test_manual_lab_real_audio_is_disabled_by_default() -> None:
    session_id, _ = await _new_named_session()

    with pytest.raises(Exception) as exc_info:
        await _send_transcribed_audio(
            session_id,
            b"audio-fixture",
            "audio/ogg",
        )

    assert getattr(exc_info.value, "status_code", None) == 503


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
    assert option_titles == ["Só o aparelho", "Compra + instalação"]


@pytest.mark.asyncio
async def test_cleaning_quantity_also_supports_six_or_more_and_exact_count() -> None:
    session_id, _ = await _new_named_session()

    snapshot = await send_message(
        session_id,
        ManualInput(text="quero fazer uma limpeza"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert [option["title"] for option in last["options"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6 ou mais",
    ]

    snapshot = await send_message(
        session_id,
        ManualInput(text="6 ou mais", action="quantity:more"),
    )
    assert snapshot["state"] == ConversationState.BOOKING_QUANTITY.value
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert "quantos itens ou aparelhos são ao todo" in last["body"].casefold()

    snapshot = await send_message(
        session_id,
        ManualInput(text="20"),
    )

    assert snapshot["context"]["quantity"] == 20
    assert snapshot["state"] != ConversationState.HUMAN_HANDOFF.value


@pytest.mark.asyncio
async def test_manual_lab_owned_installation_model_silently_checks_catalog() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "equipment_ownership": "has_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="LG Dual Inverter Voice 18000 BTU"),
    )

    assert "equipment_catalog" in snapshot["dry_run_calls"]
    assert snapshot["context"]["equipment_model"] == "LG Dual Inverter Voice 18.000 BTU"
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "não temos esse modelo específico" not in joined
    assert snapshot["state"] != ConversationState.HUMAN_HANDOFF.value


@pytest.mark.asyncio
async def test_owned_installation_model_missing_from_catalog_offers_similar_options() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "equipment_ownership": "has_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Samsung WindFree 24000 BTU"),
    )

    assert "equipment_catalog" in snapshot["dry_run_calls"]
    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assistant_messages = [
        item for item in snapshot["history"] if item["role"] == "assistant"
    ]
    joined = " ".join(item["body"] for item in assistant_messages[-3:]).casefold()
    last = assistant_messages[-1]
    assert "não encontrei esse modelo" in joined
    assert "semelhantes" in joined
    assert {option["id"] for option in last["options"]} >= {
        "equipment.model.recommend",
        "equipment.model.exact_check",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Ver semelhantes", action="equipment.model.recommend"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    assert snapshot["context"]["equipment_ownership"] == "needs_equipment"
    assert snapshot["context"]["purchase_mode"] == "both"
    assert snapshot["context"]["purchase_only"] is False
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert (
        "perfil do ambiente" in last["body"].casefold()
        or "quantas pessoas" in last["body"].casefold()
    )


@pytest.mark.asyncio
async def test_owned_installation_model_missing_from_catalog_can_keep_current_unit() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "equipment_ownership": "has_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    await send_message(
        session_id,
        ManualInput(text="Samsung WindFree 24000 BTU"),
    )
    snapshot = await send_message(
        session_id,
        ManualInput(text="Instalar este", action="equipment.model.exact_check"),
    )

    assert snapshot["automation_enabled"] is True
    assert snapshot["handoff_status"] == "none"
    assert snapshot["state"] != ConversationState.HUMAN_HANDOFF.value
    assert snapshot["context"]["equipment_ownership"] == "has_equipment"
    assert snapshot["context"]["equipment_model"] == "Samsung WindFree 24000 BTU"


@pytest.mark.asyncio
async def test_manual_lab_off_catalog_capacity_offers_grounded_similar_before_profile() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "purchase_mode": "purchase",
        "purchase_only": True,
        "equipment_ownership": "needs_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Midea 18 mil BTUs Split Hi Wall"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assert snapshot["context"]["catalog_mismatch_capacity_btu"] == 18000
    assert (
        snapshot["context"]["catalog_alternative_suggestion"]["item_id"]
        == "lab-lg-18000-cold"
    )
    assistant_messages = [
        item for item in snapshot["history"] if item["role"] == "assistant"
    ]
    joined_offer = " ".join(
        item["body"] for item in assistant_messages[-3:]
    ).casefold()
    last = assistant_messages[-1]
    assert "não temos esse modelo específico" in joined_offer
    assert "lg dual inverter voice 18.000 btu" in joined_offer
    assert {option["id"] for option in last["options"]} >= {
        "equipment.model.suggested_select",
        "equipment.model.recommend",
        "equipment.model.exact_check",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Ver outros similares", action="equipment.model.recommend"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    last_messages = [
        item["body"]
        for item in snapshot["history"]
        if item["role"] == "assistant"
    ][-2:]
    joined = " ".join(last_messages).casefold()
    assert "quantas pessoas" in joined
    assert "qual delas você gostaria de avaliar" not in joined


@pytest.mark.asyncio
async def test_manual_lab_nearest_capacity_is_explained_before_profile_questions() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "purchase_mode": "purchase",
        "purchase_only": True,
        "equipment_ownership": "needs_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Midea 50 mil BTUs"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assistant_messages = [
        item for item in snapshot["history"] if item["role"] == "assistant"
    ]
    mismatch = " ".join(
        item["body"] for item in assistant_messages[-3:]
    ).casefold()
    assert "50.000 btu" in mismatch
    assert "mais próxima em capacidade" in mismatch
    assert "18.000 btu" in mismatch
    assert "catalog_alternative_suggestion" not in snapshot["context"]

    snapshot = await send_message(
        session_id,
        ManualInput(text="Ver outros similares", action="equipment.model.recommend"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    joined = " ".join(
        item["body"]
        for item in snapshot["history"]
        if item["role"] == "assistant"
    )[-400:].casefold()
    assert "perfil do ambiente" in joined or "quantas pessoas" in joined


@pytest.mark.asyncio
async def test_manual_lab_off_catalog_model_offers_alternatives_and_keeps_automation() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "purchase_mode": "purchase",
        "purchase_only": True,
        "equipment_ownership": "needs_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="Midea 50000 BTU quente frio"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assert snapshot["automation_enabled"] is True
    assistant_messages = [
        item for item in snapshot["history"] if item["role"] == "assistant"
    ]
    joined_offer = " ".join(
        item["body"] for item in assistant_messages[-3:]
    ).casefold()
    last = assistant_messages[-1]
    assert "não temos esse modelo específico" in joined_offer
    assert "equipamentos semelhantes" in joined_offer
    assert {option["id"] for option in last["options"]} >= {
        "equipment.model.recommend",
        "equipment.model.exact_check",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(
            text="Ver semelhantes",
            action="equipment.model.recommend",
        ),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    assert snapshot["automation_enabled"] is True
    assert snapshot["handoff_status"] == "none"

    snapshot = await send_message(
        session_id,
        ManualInput(text="1"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_EQUIPMENT_PROFILE.value
    assert snapshot["automation_enabled"] is True
    assert snapshot["handoff_status"] == "none"
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert "tamanho aproximado" in last["body"].casefold()


@pytest.mark.asyncio
async def test_manual_lab_catalog_alternative_button_selects_presented_item() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "purchase_mode": "purchase",
        "purchase_only": True,
        "equipment_ownership": "needs_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    offer = await send_message(
        session_id,
        ManualInput(text="Midea 18 mil BTUs Split Hi Wall"),
    )

    assert (
        offer["context"]["catalog_alternative_suggestion"]["item_id"]
        == "lab-lg-18000-cold"
    )

    selected = await send_message(
        session_id,
        ManualInput(
            text="Escolher este",
            action="equipment.model.suggested_select",
        ),
    )

    assert selected["automation_enabled"] is True
    assert selected["handoff_status"] == "none"
    assert selected["state"] == ConversationState.BOOKING_EQUIPMENT_DELIVERY.value
    assert selected["context"]["recommended_equipment"]["item_id"] == "lab-lg-18000-cold"
    assert "LG Dual Inverter Voice" in selected["context"]["equipment_model"]


@pytest.mark.asyncio
async def test_manual_lab_partial_criteria_then_brand_never_contradicts_catalog() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_EQUIPMENT_MODEL
    session.repository.context = {
        "service_id": str(INSTALLATION_SERVICE_ID),
        "request_mode": "quote",
        "purchase_mode": "purchase",
        "purchase_only": True,
        "equipment_ownership": "needs_equipment",
        "equipment_model_known": True,
        "quantity": 1,
    }

    first = await send_message(
        session_id,
        ManualInput(text="Split Inverter 18000 BTUs"),
    )

    assert first["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assert first["context"]["equipment_request_query"] == "Split Inverter 18000 BTUs"
    first_messages = [
        item for item in first["history"] if item["role"] == "assistant"
    ]
    first_body = " ".join(
        item["body"] for item in first_messages[-3:]
    ).casefold()
    assert "ainda não identifica um modelo específico" in first_body
    assert "encontrei opções com inverter" not in first_body
    assert "não temos esse modelo específico" not in first_body

    second = await send_message(
        session_id,
        ManualInput(text="Electrolux"),
    )

    assert second["state"] == ConversationState.BOOKING_EQUIPMENT_MODEL.value
    assert second["context"]["catalog_mismatch_capacity_btu"] == 18000
    assert (
        second["context"]["catalog_alternative_suggestion"]["item_id"]
        == "lab-lg-18000-cold"
    )
    second_messages = [
        item for item in second["history"] if item["role"] == "assistant"
    ]
    second_last = second_messages[-1]
    second_body = " ".join(
        item["body"] for item in second_messages[-3:]
    ).casefold()
    assert "não temos esse modelo específico" in second_body
    assert "lg dual inverter voice 18.000 btu" in second_body
    assert "r$ 3.899,00" in second_body
    assert "encontrei esse equipamento no catálogo ativo" not in second_body
    assert {option["id"] for option in second_last["options"]} >= {
        "equipment.model.suggested_select",
        "equipment.model.recommend",
        "equipment.model.exact_check",
    }

    selected = await send_message(
        session_id,
        ManualInput(text="esse mesmo"),
    )

    assert selected["automation_enabled"] is True
    assert selected["handoff_status"] == "none"
    assert selected["state"] == ConversationState.BOOKING_EQUIPMENT_DELIVERY.value
    assert selected["context"]["recommended_equipment"]["item_id"] == "lab-lg-18000-cold"
    assert "LG Dual Inverter Voice" in selected["context"]["equipment_model"]


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

    assert snapshot["state"] == ConversationState.BOOKING_WEEKDAY.value
    assert "nome do técnico responsável" in snapshot["context"]["gate_instructions"]
    joined = " ".join(item["body"] for item in snapshot["history"]).casefold()
    assert "depois de definirmos a data e o horário" in joined
    assert "confirma qual técnico" in joined
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert [option["title"] for option in last["options"]] == [
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    ]


@pytest.mark.asyncio
async def test_manual_lab_service_address_is_requested_after_date_and_time() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_TIME
    session.repository.context = {
        "service_id": str(SERVICE_ID),
        "service_qualification_version": 2,
        "quantity": 1,
        "equipment_ownership": "has_equipment",
        "equipment_model": "Elgin Eco 9.000 BTU",
        "selected_date": "2026-11-18",
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="09:00"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_ADDRESS.value
    assert snapshot["context"]["selected_date"] == "2026-11-18"
    assert snapshot["context"]["selected_time"] == "09:00"
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert "data e horário definidos" in last["body"].casefold()
    assert "endereço completo" in last["body"].casefold()


@pytest.mark.asyncio
async def test_manual_lab_weekday_choice_offers_three_cross_month_slots() -> None:
    session_id, session = await _new_named_session()
    session.repository.state = ConversationState.BOOKING_WEEKDAY
    session.repository.context = {
        "service_id": str(SERVICE_ID),
        "service_qualification_version": 2,
    }

    snapshot = await send_message(
        session_id,
        ManualInput(text="quarta-feira"),
    )

    assert snapshot["state"] == ConversationState.BOOKING_DATE.value
    last = [item for item in snapshot["history"] if item["role"] == "assistant"][-1]
    assert [option["title"] for option in last["options"]] == [
        "quarta-feira, 18/11 às 09:00",
        "quarta-feira, 25/11 às 14:30",
        "quarta-feira, 02/12 às 18:00",
    ]


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

import pytest

from app.conversation_lab.adapters import (
    DIAGNOSTICS_SERVICE_ID,
    ELECTRICAL_SERVICE_ID,
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
from app.conversations.constants import ConversationState


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

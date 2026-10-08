from pathlib import Path
import uuid

from app.models import WebPushEvent
from app.push.service import _payload


def test_action_alert_migration_has_preferences_and_push_types() -> None:
    source = Path(
        "alembic/versions/20261007_0030_whatsapp_onboarding_preferences.py"
    ).read_text()
    assert "whatsapp_desired_mode" in source
    assert "whatsapp_review_status" in source
    assert "whatsapp_coexistence_ready" in source
    assert "billing_past_due" in source


def test_inbound_push_copy_is_for_human_intervention() -> None:
    event = WebPushEvent(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        event_key="test:handoff",
        event_type="inbound_message",
        target_path="/app/conversas/11111111-1111-1111-1111-111111111111",
    )
    payload = _payload(event)
    assert "Atendimento precisa de você" in payload
    assert "Nova mensagem" not in payload

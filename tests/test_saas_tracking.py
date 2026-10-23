from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.analytics.posthog import PostHogEvent, build_capture_payload, visitor_distinct_id
from app.api.marketing_tracking import _safe_occurred_at, _safe_properties
from app.billing.webhooks import BillingWebhookService
from app.core.config import Settings
from app.models import BillingWebhookEvent
from app.models.marketing import MarketingFunnelEvent


def test_marketing_tracking_schema_supports_idempotent_client_events() -> None:
    assert MarketingFunnelEvent.__table__.c.client_event_id.nullable is True
    assert MarketingFunnelEvent.__table__.c.occurred_at.nullable is False
    index_names = {index.name for index in MarketingFunnelEvent.__table__.indexes}
    assert "uq_marketing_funnel_events_client_event_id" in index_names
    assert "ix_marketing_funnel_events_event_occurred" in index_names
    assert "ix_marketing_funnel_events_properties_gin" in index_names


def test_marketing_properties_keep_attribution_and_event_dimensions() -> None:
    properties = {f"key_{index}": index for index in range(60)}
    safe = _safe_properties(properties)

    assert safe is not None
    assert len(safe) == 48
    assert safe["key_0"] == 0
    assert safe["key_47"] == 47
    assert "key_48" not in safe


def test_client_occurrence_time_is_bounded_for_safe_analytics() -> None:
    now = datetime.now(UTC)
    recent = now - timedelta(days=2)
    assert abs((_safe_occurred_at(recent) - recent).total_seconds()) < 1

    too_old = now - timedelta(days=120)
    assert _safe_occurred_at(too_old) > now - timedelta(minutes=1)

    future = now + timedelta(hours=1)
    assert _safe_occurred_at(future) < now + timedelta(minutes=1)


def test_billing_webhook_facts_support_recurring_revenue_analysis() -> None:
    columns = BillingWebhookEvent.__table__.c

    assert columns.provider_environment.nullable is False
    assert columns.business_id.nullable is True
    assert columns.amount_cents.nullable is True
    index_names = {index.name for index in BillingWebhookEvent.__table__.indexes}
    assert "ix_billing_webhook_events_business_received" in index_names
    assert "ix_billing_webhook_events_event_type_received" in index_names


def test_billing_webhook_amount_is_normalized_to_cents() -> None:
    assert BillingWebhookService._payment_amount_cents("197.00") == 19_700
    assert BillingWebhookService._payment_amount_cents(397) == 39_700
    assert BillingWebhookService._payment_amount_cents(0) is None
    assert BillingWebhookService._payment_amount_cents("invalid") is None


def test_posthog_export_is_disabled_without_explicit_configuration() -> None:
    settings = Settings(ENVIRONMENT="test")
    event = PostHogEvent(
        name="signup_completed",
        distinct_id=visitor_distinct_id(UUID("00000000-0000-0000-0000-000000000001")),
        insert_id="event-1",
        properties={"business_id": "business-1"},
    )

    assert build_capture_payload(settings, event) is None


def test_posthog_payload_is_idempotent_and_drops_sensitive_properties() -> None:
    settings = Settings(
        ENVIRONMENT="test",
        POSTHOG_ENABLED=True,
        POSTHOG_PROJECT_API_KEY="phc_test",
        POSTHOG_HOST="https://us.i.posthog.com",
    )
    event = PostHogEvent(
        name="whatsapp_event_outcome",
        distinct_id="user:00000000-0000-0000-0000-000000000002",
        insert_id="whatsapp:event-1:processed:automation_processed",
        properties={
            "business_id": "00000000-0000-0000-0000-000000000003",
            "outcome_reason": "automation_processed",
            "customer_message": "must-not-leave-alovia",
            "phone_number": "5511999999999",
        },
    )

    payload = build_capture_payload(settings, event)

    assert payload is not None
    assert payload["event"] == "whatsapp_event_outcome"
    assert payload["properties"]["$insert_id"] == event.insert_id
    assert payload["properties"]["business_id"].endswith("0003")
    assert payload["properties"]["outcome_reason"] == "automation_processed"
    assert "customer_message" not in payload["properties"]
    assert "phone_number" not in payload["properties"]

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.marketing_tracking import _safe_occurred_at, _safe_properties
from app.billing.webhooks import BillingWebhookService
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

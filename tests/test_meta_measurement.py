from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from pydantic import SecretStr

from app.analytics.meta_conversions import purchase_conversion_event
from app.core.config import Environment, Settings
from app.platform_admin.analytics import _effective_period_start


NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
CHECKOUT_ID = UUID("00000000-0000-0000-0000-000000000104")


def settings(**values) -> Settings:
    return Settings(ENVIRONMENT=Environment.production, **values)


def test_platform_analytics_start_at_never_expands_requested_window() -> None:
    configured = NOW - timedelta(days=2)
    current = settings(PLATFORM_ANALYTICS_START_AT=configured.isoformat())

    assert _effective_period_start(current, now=NOW, days=7) == configured
    assert _effective_period_start(current, now=NOW, days=1) == NOW - timedelta(days=1)


def test_platform_analytics_start_at_requires_timezone_to_be_effective() -> None:
    current = settings(PLATFORM_ANALYTICS_START_AT="2026-11-12T15:00:00")

    assert _effective_period_start(current, now=NOW, days=7) == NOW - timedelta(days=7)


def test_purchase_conversion_uses_checkout_id_for_meta_deduplication() -> None:
    current = settings(
        META_CAPI_ENABLED=True,
        META_CAPI_DATASET_ID="123456789012345",
        META_CAPI_ACCESS_TOKEN=SecretStr("meta-token"),
        META_CAPI_EVENT_SOURCE_BASE_URL="https://alovia.netlify.app",
    )
    checkout = SimpleNamespace(
        id=CHECKOUT_ID,
        provider_environment="production",
        status="paid",
        amount_cents=14_790,
        plan_code="basic",
    )

    event = purchase_conversion_event(
        current,
        checkout=checkout,
        occurred_at=NOW,
        owner_user_id=None,
        owner_email=None,
    )

    assert event is not None
    assert event.event_name == "Purchase"
    assert event.event_id == str(CHECKOUT_ID)
    assert event.custom_data is not None
    assert event.custom_data["order_id"] == str(CHECKOUT_ID)
    assert event.custom_data["currency"] == "BRL"
    assert event.custom_data["value"] == 147.9

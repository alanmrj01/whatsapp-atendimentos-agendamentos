from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.analytics.posthog import (
    AUTOMATIC_BOOKING_SESSION_INFO_KEY,
    PostHogEvent,
    automatic_booking_event,
    build_capture_payload,
    identity_alias_event,
    queue_automatic_booking_posthog,
    take_automatic_booking_posthog_ids,
    user_distinct_id,
    visitor_distinct_id,
)
from fastapi import Response

from app.api.marketing_tracking import (
    _decode_attribution_cookie,
    _encode_attribution_cookie,
    _resolve_attribution,
    _safe_occurred_at,
    _safe_properties,
)
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



def test_campaign_attribution_survives_navigation_without_javascript_storage() -> None:
    cookie_value = _encode_attribution_cookie(
        {
            "utm_source": "meta",
            "utm_medium": "paid_social",
            "utm_campaign": "refrigeracao_outubro",
            "campaign_id": "campaign-123",
            "adset_id": "adset-456",
            "ad_id": "ad-789",
            "landing_path": "/",
            "attribution_version": 3,
            "email": "must-not-be-stored@example.com",
        }
    )

    decoded = _decode_attribution_cookie(cookie_value)
    assert decoded["utm_source"] == "meta"
    assert decoded["utm_campaign"] == "refrigeracao_outubro"
    assert decoded["landing_path"] == "/"
    assert "email" not in decoded

    enriched, next_cookie = _resolve_attribution(
        {
            "landing_path": "/criar-conta",
            "attribution_version": 3,
        },
        cookie_value,
    )

    assert next_cookie is None
    assert enriched is not None
    assert enriched["utm_source"] == "meta"
    assert enriched["utm_campaign"] == "refrigeracao_outubro"
    assert enriched["campaign_id"] == "campaign-123"
    assert enriched["landing_path"] == "/"


def test_new_campaign_touch_replaces_previous_attribution_cookie() -> None:
    old_cookie = _encode_attribution_cookie(
        {
            "utm_source": "meta",
            "utm_campaign": "old-campaign",
            "landing_path": "/",
            "attribution_version": 3,
        }
    )

    enriched, next_cookie = _resolve_attribution(
        {
            "utm_source": "google",
            "utm_medium": "cpc",
            "utm_campaign": "new-campaign",
            "landing_path": "/",
            "attribution_version": 3,
        },
        old_cookie,
    )

    assert enriched is not None
    assert enriched["utm_source"] == "google"
    assert enriched["utm_campaign"] == "new-campaign"
    assert next_cookie is not None
    assert _decode_attribution_cookie(next_cookie)["utm_campaign"] == "new-campaign"


def test_starlette_response_accepts_attribution_cookie_configuration() -> None:
    response = Response()

    response.set_cookie(
        key="alovia_analytics_attr",
        value="encoded-attribution",
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/v1/marketing",
    )

    header = response.headers.get("set-cookie")
    assert header is not None
    assert "alovia_analytics_attr=encoded-attribution" in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "Path=/api/v1/marketing" in header


def test_invalid_attribution_cookie_is_ignored_fail_open() -> None:
    enriched, next_cookie = _resolve_attribution(
        {"landing_path": "/criar-conta", "attribution_version": 3},
        "not-valid-base64-json",
    )

    assert enriched == {"landing_path": "/criar-conta", "attribution_version": 3}
    assert next_cookie is None


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


def test_posthog_identity_alias_is_deterministic_and_contains_no_pii() -> None:
    visitor_id = UUID("00000000-0000-0000-0000-000000000011")
    user_id = UUID("00000000-0000-0000-0000-000000000012")
    event = identity_alias_event(visitor_id, user_id)

    assert event.name == "$create_alias"
    assert event.distinct_id == visitor_distinct_id(visitor_id)
    assert event.properties == {"alias": user_distinct_id(user_id)}
    assert event.process_person_profile is True
    assert event.insert_id == (
        f"identity:{visitor_distinct_id(visitor_id)}:{user_distinct_id(user_id)}"
    )

    settings = Settings(
        ENVIRONMENT="test",
        POSTHOG_ENABLED=True,
        POSTHOG_PROJECT_API_KEY="phc_test",
        POSTHOG_HOST="https://us.i.posthog.com",
    )
    payload = build_capture_payload(settings, event)
    assert payload is not None
    assert payload["event"] == "$create_alias"
    assert payload["distinct_id"] == visitor_distinct_id(visitor_id)
    assert payload["properties"]["alias"] == user_distinct_id(user_id)
    assert payload["properties"]["$process_person_profile"] is True


class _SessionInfoStub:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}


def test_automatic_booking_outbox_is_deduplicated_and_drained_once() -> None:
    session = _SessionInfoStub()
    appointment_id = UUID("00000000-0000-0000-0000-000000000021")

    queue_automatic_booking_posthog(session, appointment_id)  # type: ignore[arg-type]
    queue_automatic_booking_posthog(session, appointment_id)  # type: ignore[arg-type]

    assert session.info[AUTOMATIC_BOOKING_SESSION_INFO_KEY] == {appointment_id}
    assert take_automatic_booking_posthog_ids(session) == [appointment_id]  # type: ignore[arg-type]
    assert take_automatic_booking_posthog_ids(session) == []  # type: ignore[arg-type]


def test_automatic_booking_event_is_authoritative_idempotent_and_pii_free() -> None:
    appointment_id = UUID("00000000-0000-0000-0000-000000000031")
    business_id = UUID("00000000-0000-0000-0000-000000000032")
    service_id = UUID("00000000-0000-0000-0000-000000000033")
    owner_user_id = UUID("00000000-0000-0000-0000-000000000034")

    event = automatic_booking_event(
        appointment_id=appointment_id,
        business_id=business_id,
        service_id=service_id,
        owner_user_id=owner_user_id,
    )

    assert event.name == "automatic_booking_confirmed"
    assert event.distinct_id == user_distinct_id(owner_user_id)
    assert event.insert_id == f"automatic_booking:{appointment_id}"
    assert event.process_person_profile is True
    assert event.properties == {
        "appointment_id": str(appointment_id),
        "business_id": str(business_id),
        "service_id": str(service_id),
        "booking_source": "whatsapp_automation",
    }
    assert all(
        fragment not in " ".join(event.properties).casefold()
        for fragment in ("email", "phone", "telefone", "message", "whatsapp_id")
    )

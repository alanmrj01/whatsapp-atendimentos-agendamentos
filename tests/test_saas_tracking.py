from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from app.analytics.meta_conversions import (
    build_meta_payload,
    client_ip_address,
    marketing_conversion_event,
    purchase_conversion_event,
)
from app.analytics.posthog import (
    AUTOMATIC_BOOKING_SESSION_INFO_KEY,
    PostHogEvent,
    automatic_booking_event,
    build_capture_payload,
    identity_alias_event,
    queue_automatic_booking_posthog,
    subscription_paid_event,
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
from app.models import BillingCheckout, BillingWebhookEvent
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

def test_subscription_paid_event_is_authoritative_idempotent_and_revenue_ready() -> None:
    business_id = UUID("00000000-0000-0000-0000-000000000041")
    checkout = BillingCheckout(
        id=UUID("00000000-0000-0000-0000-000000000042"),
        business_id=business_id,
        idempotency_key=UUID("00000000-0000-0000-0000-000000000043"),
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        provider_environment="production",
        amount_cents=19_700,
        status="paid",
    )
    owner_user_id = UUID("00000000-0000-0000-0000-000000000044")
    occurred_at = datetime.now(UTC)

    event = subscription_paid_event(
        checkout=checkout,
        owner_user_id=owner_user_id,
        occurred_at=occurred_at,
    )

    assert event.name == "subscription_paid"
    assert event.insert_id == f"subscription_paid:{checkout.id}"
    assert event.distinct_id == user_distinct_id(owner_user_id)
    assert event.occurred_at == occurred_at
    assert event.properties["business_id"] == str(business_id)
    assert event.properties["plan"] == "basic"
    assert event.properties["billing_cycle"] == "monthly"
    assert event.properties["payment_method"] == "pix"
    assert event.properties["amount_cents"] == 19_700
    assert event.properties["funnel_stage"] == "subscription_paid"
    assert "email" not in event.properties
    assert "phone" not in event.properties

def _meta_settings(**overrides) -> Settings:
    values = {
        "ENVIRONMENT": "production",
        "META_CAPI_ENABLED": True,
        "META_CAPI_DATASET_ID": "1432131901730735",
        "META_CAPI_ACCESS_TOKEN": "meta-secret-token",
        "META_CAPI_GRAPH_VERSION": "v26.0",
        "META_CAPI_EVENT_SOURCE_BASE_URL": "https://alovia.netlify.app",
    }
    values.update(overrides)
    return Settings(**values)


def test_meta_capi_is_fail_closed_outside_explicit_production_configuration() -> None:
    assert Settings(ENVIRONMENT="test").meta_conversions_configuration() is None
    assert Settings(
        ENVIRONMENT="production",
        META_CAPI_ENABLED=True,
    ).meta_conversions_configuration() is None


def test_meta_capi_registration_uses_shared_event_id_and_hashed_match_data() -> None:
    settings = _meta_settings(META_CAPI_TEST_EVENT_CODE="TEST123")
    visitor_id = UUID("00000000-0000-0000-0000-000000000051")
    user_id = UUID("00000000-0000-0000-0000-000000000052")
    event_id = UUID("00000000-0000-0000-0000-000000000053")
    occurred_at = datetime(2026, 10, 29, 23, 0, tzinfo=UTC)

    event = marketing_conversion_event(
        settings,
        event_name="signup_completed",
        event_id=event_id,
        occurred_at=occurred_at,
        path="/criar-conta",
        properties={"funnel_stage": "registration_completed"},
        visitor_id=visitor_id,
        user_id=user_id,
        user_email="Owner@Example.com ",
        fbp="fb.1.1234567890.123456789",
        fbc="fb.1.1234567890.TESTFBCLID",
        client_ip="203.0.113.10",
        client_user_agent="Alovia Browser",
    )

    assert event is not None
    assert event.event_name == "CompleteRegistration"
    assert event.event_id == str(event_id)
    assert event.event_source_url == "https://alovia.netlify.app/criar-conta"
    assert event.user_data["em"] == [
        sha256(b"owner@example.com").hexdigest()
    ]
    assert event.user_data["external_id"] == [
        sha256(str(user_id).encode()).hexdigest()
    ]
    assert event.user_data["fbp"] == "fb.1.1234567890.123456789"
    assert event.user_data["fbc"] == "fb.1.1234567890.TESTFBCLID"
    assert event.user_data["client_ip_address"] == "203.0.113.10"
    assert "Owner@Example.com" not in str(event.user_data)

    configuration = settings.meta_conversions_configuration()
    assert configuration is not None
    payload = build_meta_payload(configuration, event)
    assert payload["data"][0]["event_name"] == "CompleteRegistration"
    assert payload["data"][0]["event_id"] == str(event_id)
    assert payload["data"][0]["action_source"] == "website"
    assert payload["access_token"] == "meta-secret-token"
    assert payload["test_event_code"] == "TEST123"


def test_meta_capi_whitelist_rejects_internal_or_non_authoritative_events() -> None:
    settings = _meta_settings()
    common = {
        "settings": settings,
        "event_id": UUID("00000000-0000-0000-0000-000000000054"),
        "occurred_at": datetime.now(UTC),
        "path": "/app",
        "visitor_id": UUID("00000000-0000-0000-0000-000000000055"),
        "user_id": None,
        "user_email": None,
        "fbp": None,
        "fbc": None,
        "client_ip": "203.0.113.11",
        "client_user_agent": "Browser",
    }

    assert marketing_conversion_event(
        **common,
        event_name="page_view",
        properties={"route_group": "conversations"},
    ) is None
    assert marketing_conversion_event(
        **common,
        event_name="payment_confirmed",
        properties={"checkout_id": "should-not-be-used"},
    ) is None
    assert marketing_conversion_event(
        **common,
        event_name="landing_vsl_progress",
        properties={"percent": 33},
    ) is None


def test_meta_capi_purchase_is_authoritative_and_uses_exact_checkout_value() -> None:
    settings = _meta_settings()
    checkout = BillingCheckout(
        id=UUID("00000000-0000-0000-0000-000000000056"),
        business_id=UUID("00000000-0000-0000-0000-000000000057"),
        idempotency_key=UUID("00000000-0000-0000-0000-000000000058"),
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        provider_environment="production",
        amount_cents=19_700,
        status="paid",
    )
    owner_id = UUID("00000000-0000-0000-0000-000000000059")

    event = purchase_conversion_event(
        settings,
        checkout=checkout,
        occurred_at=datetime(2026, 10, 29, 23, 5, tzinfo=UTC),
        owner_user_id=owner_id,
        owner_email="owner@example.com",
    )

    assert event is not None
    assert event.event_name == "Purchase"
    assert event.event_id == str(checkout.id)
    assert event.custom_data == {
        "currency": "BRL",
        "value": 197.0,
        "order_id": str(checkout.id),
        "content_name": "basic",
        "content_type": "product",
        "content_ids": ["basic"],
    }
    assert event.user_data["em"] == [sha256(b"owner@example.com").hexdigest()]

    checkout.provider_environment = "sandbox"
    assert purchase_conversion_event(
        settings,
        checkout=checkout,
        occurred_at=datetime.now(UTC),
        owner_user_id=owner_id,
        owner_email="owner@example.com",
    ) is None


def test_meta_capi_client_ip_uses_first_valid_forwarded_address() -> None:
    assert client_ip_address(
        "203.0.113.20, 10.0.0.2",
        "127.0.0.1",
    ) == "203.0.113.20"
    assert client_ip_address("not-an-ip", "198.51.100.4") == "198.51.100.4"


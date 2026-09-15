from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.billing.asaas import AsaasGateway
from app.billing.catalog import get_offer
from app.billing.service import _cycle_end
from app.billing.webhooks import SUPPORTED_EVENTS
from app.core.config import AsaasConfigurationError, Environment, Settings
from app.models import BillingCheckout, BillingWebhookEvent, CommercialSubscription


def settings(**values):
    return Settings(ENVIRONMENT=Environment.test, **values)


def test_server_side_catalog_uses_approved_prices_and_cycles() -> None:
    assert get_offer("basic", "monthly").amount_cents == 19_700
    assert get_offer("basic", "quarterly").amount_cents == 53_190
    assert get_offer("basic", "annual").amount_cents == 200_940
    assert get_offer("plus", "monthly").amount_cents == 29_700
    assert get_offer("plus", "quarterly").amount_cents == 80_190
    assert get_offer("plus", "annual").amount_cents == 302_940
    assert get_offer("basic", "quarterly").asaas_cycle == "QUARTERLY"
    assert get_offer("plus", "annual").asaas_cycle == "YEARLY"


def test_asaas_key_selects_matching_environment_without_frontend_secret() -> None:
    sandbox = settings(ASAAS_API_KEY=SecretStr("$aact_hmlg_example"))
    config = sandbox.require_asaas_configuration()
    assert config.api_base_url == "https://api-sandbox.asaas.com/v3"
    assert config.checkout_base_url == "https://sandbox.asaas.com"

    production = settings(ASAAS_API_KEY=SecretStr("$aact_prod_example"))
    config = production.require_asaas_configuration()
    assert config.api_base_url == "https://api.asaas.com/v3"
    assert config.checkout_base_url == "https://asaas.com"

    with pytest.raises(AsaasConfigurationError):
        settings(ASAAS_API_KEY=SecretStr("invalid")).require_asaas_configuration()


def test_webhook_token_requires_dedicated_high_entropy_secret() -> None:
    with pytest.raises(AsaasConfigurationError):
        settings(ASAAS_WEBHOOK_TOKEN=SecretStr("short")).require_asaas_webhook_token()
    token = "x" * 40
    assert settings(ASAAS_WEBHOOK_TOKEN=SecretStr(token)).require_asaas_webhook_token() == token


def test_checkout_url_is_restricted_to_configured_asaas_host() -> None:
    gateway = AsaasGateway(
        settings(ASAAS_API_KEY=SecretStr("$aact_hmlg_example")).require_asaas_configuration()
    )
    assert gateway._safe_checkout_url("https://sandbox.asaas.com/checkoutSession/show/abc")
    assert not gateway._safe_checkout_url("https://evil.example/checkoutSession/show/abc")
    assert not gateway._safe_checkout_url("http://sandbox.asaas.com/checkoutSession/show/abc")


def test_cycle_end_uses_calendar_months() -> None:
    anchor = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert _cycle_end(anchor, "monthly") == datetime(2026, 2, 28, 12, 0, tzinfo=UTC)
    assert _cycle_end(anchor, "quarterly") == datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    assert _cycle_end(anchor, "annual") == datetime(2027, 1, 31, 12, 0, tzinfo=UTC)


def test_billing_tables_are_separate_from_admin_entitlement() -> None:
    assert BillingCheckout.__tablename__ == "billing_checkouts"
    assert CommercialSubscription.__tablename__ == "commercial_subscriptions"
    assert BillingWebhookEvent.__tablename__ == "billing_webhook_events"
    assert "CHECKOUT_PAID" in SUPPORTED_EVENTS
    assert "SUBSCRIPTION_CREATED" in SUPPORTED_EVENTS
    assert "PAYMENT_CONFIRMED" in SUPPORTED_EVENTS

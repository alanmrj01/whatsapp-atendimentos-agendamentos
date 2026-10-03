from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from app.billing.asaas import AsaasGateway
from app.billing.catalog import (
    BillingCatalogConfigurationError,
    get_offer,
    payment_method_is_enabled,
    plan_is_enabled,
)
from app.billing.schemas import CheckoutCreateRequest
from app.billing.service import BillingService, _cycle_end, _payment_value_cents
from app.billing.webhooks import BillingWebhookService, SUPPORTED_EVENTS
from app.core.config import AsaasConfigurationError, Environment, Settings
from app.models import BillingCheckout, BillingWebhookEvent, CommercialSubscription


def settings(**values):
    return Settings(ENVIRONMENT=Environment.test, **values)


def test_sandbox_price_override_is_isolated(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    assert get_offer("basic", "monthly").amount_cents == 500
    assert get_offer("plus", "annual").amount_cents == 500

    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "100")
    assert get_offer("basic", "monthly").amount_cents == 19_700


def test_plus_is_launch_gated(monkeypatch) -> None:
    monkeypatch.delenv("BILLING_PLUS_ENABLED", raising=False)
    assert plan_is_enabled("basic") is True
    assert plan_is_enabled("plus") is False

    monkeypatch.setenv("BILLING_PLUS_ENABLED", "true")
    assert plan_is_enabled("plus") is True


def test_pix_automatic_is_fail_closed_in_production(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    monkeypatch.delenv("BILLING_PIX_AUTOMATIC_ENABLED", raising=False)

    assert payment_method_is_enabled("credit_card") is True
    assert payment_method_is_enabled("pix_automatic") is False

    monkeypatch.setenv("BILLING_PIX_AUTOMATIC_ENABLED", "true")
    assert payment_method_is_enabled("pix_automatic") is True

    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.delenv("BILLING_PIX_AUTOMATIC_ENABLED", raising=False)
    assert payment_method_is_enabled("pix_automatic") is True


@pytest.mark.asyncio
async def test_production_pix_checkout_is_rejected_before_provider_call(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    monkeypatch.delenv("BILLING_PIX_AUTOMATIC_ENABLED", raising=False)

    class DbMustNotBeTouched:
        async def scalar(self, *args, **kwargs):
            raise AssertionError("Pix gate should run before database access")

    payload = CheckoutCreateRequest(
        plan="basic",
        cycle="monthly",
        payment_method="pix_automatic",
        return_origin="https://alovia.netlify.app",
        payer_name="Empresa Teste",
        payer_cpf_cnpj="12345678000195",
    )

    service = BillingService(DbMustNotBeTouched(), object())

    with pytest.raises(HTTPException) as error:
        await service.create_checkout(
            business_id=uuid4(),
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=payload,
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Payment method is not available yet"


def test_sandbox_price_override_rejects_provider_invalid_amount(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "499")
    with pytest.raises(BillingCatalogConfigurationError):
        get_offer("basic", "monthly")


def test_server_side_catalog_uses_approved_prices_and_cycles() -> None:
    assert get_offer("basic", "monthly").amount_cents == 19_700
    assert get_offer("basic", "quarterly").amount_cents == 53_190
    assert get_offer("basic", "annual").amount_cents == 200_940
    assert get_offer("plus", "monthly").amount_cents == 39_700
    assert get_offer("plus", "quarterly").amount_cents == 107_190
    assert get_offer("plus", "annual").amount_cents == 404_940
    assert get_offer("basic", "quarterly").asaas_cycle == "QUARTERLY"
    assert get_offer("plus", "annual").asaas_cycle == "YEARLY"
    assert get_offer("basic", "monthly").asaas_pix_frequency == "MONTHLY"
    assert get_offer("basic", "quarterly").asaas_pix_frequency == "QUARTERLY"
    assert get_offer("plus", "annual").asaas_pix_frequency == "ANNUALLY"


def test_pix_checkout_requires_only_minimum_payer_identity() -> None:
    request = CheckoutCreateRequest(
        plan="basic",
        cycle="quarterly",
        payment_method="pix_automatic",
        return_origin="https://alovia.netlify.app",
        payer_name="  Empresa   Teste  ",
        payer_cpf_cnpj="12.345.678/0001-95",
    )
    assert request.payer_name == "Empresa Teste"
    assert request.payer_cpf_cnpj == "12345678000195"

    with pytest.raises(ValidationError):
        CheckoutCreateRequest(
            plan="basic",
            cycle="quarterly",
            payment_method="pix_automatic",
            return_origin="https://alovia.netlify.app",
        )


def test_card_checkout_does_not_require_document() -> None:
    request = CheckoutCreateRequest(
        plan="plus",
        cycle="annual",
        payment_method="credit_card",
        return_origin="https://alovia.netlify.app",
    )
    assert request.payer_name is None
    assert request.payer_cpf_cnpj is None


def test_provider_amount_is_compared_in_cents_without_float_trust() -> None:
    assert _payment_value_cents("531.90") == 53_190
    assert _payment_value_cents(297) == 29_700
    assert _payment_value_cents("0") is None
    assert _payment_value_cents("invalid") is None


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



@pytest.mark.asyncio
async def test_sandbox_billing_never_marks_real_operational_history(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")

    class DbMustNotBeTouched:
        async def get(self, *args, **kwargs):
            raise AssertionError("Sandbox attempted to mutate BusinessAccess")

    service = BillingService(DbMustNotBeTouched(), object())
    await service._record_operational_history(uuid4())




def test_webhook_event_id_is_namespaced_by_provider_environment() -> None:
    assert (
        BillingWebhookService._event_storage_id("sandbox", "evt_123")
        == "sandbox:evt_123"
    )
    assert (
        BillingWebhookService._event_storage_id("production", "evt_123")
        == "evt_123"
    )



def test_billing_tables_and_webhooks_cover_both_payment_methods() -> None:
    assert BillingCheckout.__tablename__ == "billing_checkouts"
    assert CommercialSubscription.__tablename__ == "commercial_subscriptions"
    assert BillingWebhookEvent.__tablename__ == "billing_webhook_events"
    assert "CHECKOUT_PAID" in SUPPORTED_EVENTS
    assert "SUBSCRIPTION_CREATED" in SUPPORTED_EVENTS
    assert "PAYMENT_CONFIRMED" in SUPPORTED_EVENTS
    assert "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED" in SUPPORTED_EVENTS
    assert "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED" in SUPPORTED_EVENTS
    assert "PIX_AUTOMATIC_RECURRING_PAYMENT_INSTRUCTION_SCHEDULED" in SUPPORTED_EVENTS


@pytest.mark.asyncio
async def test_pix_automatic_sets_immediate_qr_expiration() -> None:
    from types import SimpleNamespace

    captured = {}

    class Gateway:
        async def find_customer(self, **kwargs):
            return "cus_test"

        async def create_customer(self, **kwargs):
            raise AssertionError("Cliente existente não deveria ser recriado")

        async def create_pix_authorization(self, payload):
            captured.update(payload)
            return SimpleNamespace(
                authorization_id="auth_test",
                subscription_id="sub_pix_test",
                payload="pix-payload",
                conciliation_identifier="conciliation-test",
                expires_at=None,
            )

    checkout = SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        provider_customer_id=None,
        provider_authorization_id=None,
        provider_subscription_id=None,
        pix_qr_payload=None,
        pix_conciliation_identifier=None,
        pix_qr_expires_at=None,
        expires_at=None,
    )

    service = BillingService(object(), Gateway())

    await service._prepare_pix_automatic(
        checkout=checkout,
        offer=get_offer("basic", "monthly"),
        payer_name="Empresa Teste",
        payer_cpf_cnpj="12345678000195",
        payer_email="teste@example.com",
    )

    assert captured["immediateQrCode"] == {
        "originalValue": 197.0,
        "expirationSeconds": 3600,
    }
    assert checkout.provider_subscription_id == "sub_pix_test"



@pytest.mark.asyncio
async def test_pix_gateway_reads_top_level_payload_from_asaas_response() -> None:
    gateway = AsaasGateway(
        settings(
            ASAAS_API_KEY=SecretStr("$aact_hmlg_example")
        ).require_asaas_configuration()
    )

    async def fake_json_request(*args, **kwargs):
        return {
            "id": "auth_test_123",
            "subscriptionId": "sub_pix_test",
            "payload": "000201pix-copia-e-cola",
            "encodedImage": "base64-image",
            "immediateQrCode": {
                "conciliationIdentifier": "conciliation-test",
                "expirationDate": "2026-09-16 01:00:00",
            },
        }

    gateway._json_request = fake_json_request  # type: ignore[method-assign]

    result = await gateway.create_pix_authorization({})

    assert result.authorization_id == "auth_test_123"
    assert result.subscription_id == "sub_pix_test"
    assert result.payload == "000201pix-copia-e-cola"
    assert result.conciliation_identifier == "conciliation-test"
    assert result.expires_at is not None


class _BillingStateDb:
    def __init__(self, checkout):
        self.checkout = checkout
        self.subscription = None
        self.commits = 0

    async def scalar(self, statement):
        sql = str(statement)
        if "FROM billing_checkouts" in sql:
            return self.checkout
        if "FROM commercial_subscriptions" in sql:
            if self.subscription is None:
                return None
            if "checkout_id" in sql:
                return (
                    self.subscription
                    if self.subscription.checkout_id == self.checkout.id
                    else None
                )
            if "provider_subscription_id" in sql:
                return (
                    self.subscription
                    if self.subscription.provider_subscription_id
                    == self.checkout.provider_subscription_id
                    else None
                )
            return self.subscription
        return None

    def add(self, obj):
        if isinstance(obj, CommercialSubscription):
            self.subscription = obj

    async def commit(self):
        self.commits += 1


class _GatewayMustNotBeCalled:
    def __getattr__(self, name):
        raise AssertionError(f"Webhook reconciliation called provider gateway: {name}")


def _card_checkout():
    return SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        payment_method="credit_card",
        provider_environment="sandbox",
        provider_checkout_id="checkout_test",
        provider_subscription_id=None,
        provider_customer_id=None,
        plan_code="basic",
        billing_cycle="monthly",
        amount_cents=500,
        status="active",
        paid_at=None,
    )


@pytest.mark.asyncio
async def test_card_webhooks_correlate_payment_before_checkout_paid(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _card_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_payment_event(
        "PAYMENT_CREATED",
        {
            "id": "pay_test",
            "checkoutSession": "checkout_test",
            "subscription": "sub_test",
            "customer": "cus_test",
            "value": 5.0,
        },
    )
    assert checkout.provider_subscription_id == "sub_test"
    assert checkout.provider_customer_id == "cus_test"
    assert checkout.status == "active"
    assert db.subscription is None

    await service.activate_paid_checkout("checkout_test")

    assert checkout.status == "paid"
    assert checkout.paid_at is not None
    assert db.subscription is not None
    assert db.subscription.status == "active"
    assert db.subscription.provider_subscription_id == "sub_test"
    assert db.subscription.provider_customer_id == "cus_test"


@pytest.mark.asyncio
async def test_card_webhooks_correlate_checkout_paid_before_payment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _card_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.activate_paid_checkout("checkout_test")
    assert checkout.status == "paid"
    assert db.subscription is None

    await service.apply_payment_event(
        "PAYMENT_CREATED",
        {
            "id": "pay_test",
            "checkoutSession": "checkout_test",
            "subscription": "sub_test",
            "customer": "cus_test",
            "value": "5.00",
        },
    )

    assert db.subscription is not None
    assert db.subscription.status == "active"
    assert db.subscription.provider_subscription_id == "sub_test"


@pytest.mark.asyncio
async def test_card_payment_amount_mismatch_never_links_subscription(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _card_checkout()
    checkout.status = "paid"
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_payment_event(
        "PAYMENT_CREATED",
        {
            "checkoutSession": "checkout_test",
            "subscription": "sub_wrong",
            "customer": "cus_test",
            "value": 6.0,
        },
    )

    assert checkout.provider_subscription_id is None
    assert db.subscription is None



@pytest.mark.asyncio
async def test_pix_activation_persists_provider_subscription_id(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")

    checkout = SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        payment_method="pix_automatic",
        provider_environment="sandbox",
        provider_authorization_id="auth_pix_test",
        provider_subscription_id=None,
        provider_customer_id="cus_pix_test",
        plan_code="basic",
        billing_cycle="monthly",
        amount_cents=500,
        status="active",
        paid_at=None,
    )
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED",
        {
            "id": "auth_pix_test",
            "customerId": "cus_pix_test",
            "value": 5.0,
            "subscriptionId": "sub_pix_test",
        },
    )

    assert checkout.status == "paid"
    assert checkout.provider_subscription_id == "sub_pix_test"
    assert db.subscription is not None
    assert db.subscription.status == "active"
    assert db.subscription.provider_authorization_id == "auth_pix_test"
    assert db.subscription.provider_subscription_id == "sub_pix_test"

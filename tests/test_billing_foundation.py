from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from app.billing.asaas import AsaasGateway, AsaasGatewayError, AsaasGatewayTimeoutError
from app.billing.catalog import (
    BillingCatalogConfigurationError,
    get_offer,
    native_card_checkout_is_enabled,
    payment_method_is_enabled,
    plan_is_enabled,
)
from app.billing.schemas import (
    CheckoutCreateRequest,
    CheckoutProfileResponse,
    CreditCardCheckoutRequest,
)
from app.billing.service import BillingService, _cycle_end, _payment_value_cents
from app.billing.webhooks import BillingWebhookService, SUPPORTED_EVENTS
from app.core.config import AsaasConfigurationError, Environment, Settings
from app.models import BillingCheckout, BillingWebhookEvent, BusinessAccess, CommercialSubscription, WebPushEvent


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


def test_checkout_profile_exposes_runtime_payment_capabilities(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    monkeypatch.setenv("BILLING_PIX_AUTOMATIC_ENABLED", "false")
    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")

    profile = CheckoutProfileResponse(
        email="teste@example.com",
        business_name="Empresa Teste",
        pix_automatic_available=payment_method_is_enabled("pix_automatic"),
        native_card_checkout_available=native_card_checkout_is_enabled(),
    )

    assert profile.pix_automatic_available is False
    assert profile.native_card_checkout_available is True


def test_native_card_checkout_is_launch_gated(monkeypatch) -> None:
    monkeypatch.delenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", raising=False)
    assert native_card_checkout_is_enabled() is False

    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")
    assert native_card_checkout_is_enabled() is True


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


def test_official_asaas_sandbox_card_is_accepted_by_schema() -> None:
    request = CreditCardCheckoutRequest(
        payer_name="Teste Alovia",
        payer_cpf_cnpj="12345678909",
        payer_postal_code="12235740",
        payer_address_number="160",
        payer_phone="11999999999",
        card_holder_name="TESTE ALOVIA",
        card_number="4444 4444 4444 4444",
        card_expiry_month="10",
        card_expiry_year="2030",
        card_ccv="123",
    )
    assert request.card_number.get_secret_value() == "4444444444444444"


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
        self.push_events = []
        self.commits = 0
        self.rollbacks = 0
        self.locks = 0
        self.lock_keys: list[str] = []
        self.subscription_adds = 0

    async def execute(self, statement, parameters):
        assert "pg_advisory_xact_lock" in str(statement)
        assert parameters["lock_key"] == (
            f"alovia:billing:{self.checkout.provider_environment}:"
            f"{self.checkout.business_id}"
        )
        self.locks += 1

    async def scalar(self, statement):
        sql = str(statement)
        if "FROM billing_checkouts" in sql:
            return self.checkout
        if "FROM commercial_subscriptions" in sql:
            if self.subscription is None:
                return None
            if "commercial_subscriptions.checkout_id =" in sql:
                return (
                    self.subscription
                    if self.subscription.checkout_id == self.checkout.id
                    else None
                )
            if "commercial_subscriptions.provider_subscription_id =" in sql:
                return (
                    self.subscription
                    if self.subscription.provider_subscription_id
                    == self.checkout.provider_subscription_id
                    else None
                )
            if "commercial_subscriptions.provider_authorization_id =" in sql:
                return (
                    self.subscription
                    if self.subscription.provider_authorization_id
                    == self.checkout.provider_authorization_id
                    else None
                )
            return self.subscription
        return None

    def add(self, obj):
        if isinstance(obj, CommercialSubscription):
            self.subscription = obj
            self.subscription_adds += 1
        elif isinstance(obj, WebPushEvent):
            self.push_events.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def get(self, model, identifier):
        if model is BillingCheckout and identifier == self.checkout.id:
            return self.checkout
        return None


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
async def test_overdue_payment_creates_one_action_alert(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _card_checkout()
    checkout.provider_subscription_id = "sub_overdue"
    db = _BillingStateDb(checkout)
    db.subscription = SimpleNamespace(
        id=uuid4(),
        checkout_id=checkout.id,
        business_id=checkout.business_id,
        provider_subscription_id="sub_overdue",
        provider_authorization_id=None,
        provider_customer_id="cus_overdue",
        provider_environment="sandbox",
        payment_method="credit_card",
        plan_code="basic",
        billing_cycle="monthly",
        status="active",
        access_until=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    service = BillingService(db, _GatewayMustNotBeCalled())

    event_key = await service.apply_payment_event(
        "PAYMENT_OVERDUE",
        {
            "id": "pay_overdue_123",
            "subscription": "sub_overdue",
            "customer": "cus_overdue",
            "value": 197.0,
        },
    )

    assert event_key == "billing-overdue:sandbox:pay_overdue_123"
    assert db.subscription.status == "past_due"
    assert len(db.push_events) == 1
    assert db.push_events[0].event_type == "billing_action"
    assert db.push_events[0].target_path == "/app/mais/plano"

    second = await service.apply_payment_event(
        "PAYMENT_OVERDUE",
        {
            "id": "pay_overdue_123",
            "subscription": "sub_overdue",
            "customer": "cus_overdue",
            "value": 197.0,
        },
    )
    assert second is None
    assert len(db.push_events) == 1


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


@pytest.mark.asyncio
async def test_hosted_checkout_uses_ten_minute_expiration() -> None:
    captured = {}

    class Gateway:
        async def create_checkout(self, payload):
            captured.update(payload)
            return SimpleNamespace(
                checkout_id="checkout_test",
                checkout_url="https://sandbox.asaas.com/checkoutSession/show?id=checkout_test",
            )

    checkout = SimpleNamespace(id=uuid4(), provider_checkout_id=None, checkout_url=None)
    await BillingService(object(), Gateway())._prepare_credit_card(
        checkout=checkout,
        offer=get_offer("basic", "monthly"),
        return_origin="https://alovia.netlify.app",
    )

    assert captured["minutesToExpire"] == 10
    assert captured["items"][0]["description"] == "Assinatura Basic - mensal"


@pytest.mark.asyncio
async def test_admin_full_access_blocks_native_card_before_provider(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")

    business_id = uuid4()
    checkout_id = uuid4()

    class Db:
        async def get(self, model, identifier):
            if model is BusinessAccess and identifier == business_id:
                return SimpleNamespace(admin_full_access=True)
            raise AssertionError("Checkout/provider state must not be read after admin grant")

    class Gateway:
        def __getattr__(self, name):
            raise AssertionError(f"Provider gateway must not be called: {name}")

    request = CreditCardCheckoutRequest(
        payer_name="Teste Alovia",
        payer_cpf_cnpj="12345678909",
        payer_postal_code="12235740",
        payer_address_number="160",
        payer_phone="11999999999",
        card_holder_name="TESTE ALOVIA",
        card_number="4444444444444444",
        card_expiry_month="10",
        card_expiry_year="2030",
        card_ccv="123",
    )

    with pytest.raises(HTTPException) as error:
        await BillingService(Db(), Gateway()).pay_credit_card(
            business_id=business_id,
            payer_email="teste@example.com",
            checkout_id=checkout_id,
            payload=request,
            remote_ip="203.0.113.10",
        )

    assert error.value.status_code == 409
    assert error.value.detail == (
        "Admin full access is active; a paid plan is not required"
    )


@pytest.mark.asyncio
async def test_legacy_admin_paid_access_blocks_native_card_before_provider(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")

    business_id = uuid4()
    checkout_id = uuid4()

    class Db:
        async def get(self, model, identifier):
            if model is BusinessAccess and identifier == business_id:
                return SimpleNamespace(admin_full_access=False, access_mode="paid")
            raise AssertionError("Checkout/provider state must not be read after admin grant")

    class Gateway:
        def __getattr__(self, name):
            raise AssertionError(f"Provider gateway must not be called: {name}")

    request = CreditCardCheckoutRequest(
        payer_name="Teste Alovia",
        payer_cpf_cnpj="12345678909",
        payer_postal_code="12235740",
        payer_address_number="160",
        payer_phone="11999999999",
        card_holder_name="TESTE ALOVIA",
        card_number="4444444444444444",
        card_expiry_month="10",
        card_expiry_year="2030",
        card_ccv="123",
    )

    with pytest.raises(HTTPException) as error:
        await BillingService(Db(), Gateway()).pay_credit_card(
            business_id=business_id,
            payer_email="teste@example.com",
            checkout_id=checkout_id,
            payload=request,
            remote_ip="203.0.113.10",
        )

    assert error.value.status_code == 409
    assert error.value.detail == (
        "Admin full access is active; a paid plan is not required"
    )


class _CheckoutCreateDb:
    def __init__(self) -> None:
        self.checkouts: list[BillingCheckout] = []
        self.subscription = None
        self.commits = 0
        self.rollbacks = 0
        self.locks = 0
        self.lock_keys: list[str] = []

    async def execute(self, statement, parameters):
        assert "pg_advisory_xact_lock" in str(statement)
        assert parameters["lock_key"].startswith("alovia:billing:sandbox:")
        self.locks += 1
        self.lock_keys.append(parameters["lock_key"])

    async def scalar(self, statement):
        sql = str(statement)
        params = statement.compile().params
        if "billing_checkouts.idempotency_key =" in sql:
            key = next(
                value
                for name, value in params.items()
                if name.startswith("idempotency_key_")
            )
            business_id = next(
                (
                    value
                    for name, value in params.items()
                    if name.startswith("business_id_")
                ),
                None,
            )
            return next(
                (
                    checkout
                    for checkout in self.checkouts
                    if checkout.idempotency_key == key
                    and (business_id is None or checkout.business_id == business_id)
                ),
                None,
            )
        if "FROM commercial_subscriptions" in sql:
            return self.subscription
        if "FROM billing_checkouts" in sql:
            business_id = next(
                (
                    value
                    for name, value in params.items()
                    if name.startswith("business_id_")
                ),
                None,
            )
            candidates = [
                checkout
                for checkout in self.checkouts
                if checkout.provider_environment == "sandbox"
                and (business_id is None or checkout.business_id == business_id)
                and (
                    checkout.status in {"creating", "active"}
                    or (
                        checkout.payment_method == "pix_automatic"
                        and checkout.paid_at is not None
                        and checkout.status != "paid"
                    )
                    or (
                        checkout.payment_method == "pix_automatic"
                        and checkout.status == "expired"
                        and checkout.provider_authorization_id is not None
                    )
                )
            ]
            return candidates[-1] if candidates else None
        return None

    async def get(self, model, identifier):
        if model is BillingCheckout:
            return next(
                (checkout for checkout in self.checkouts if checkout.id == identifier),
                None,
            )
        return None

    def add(self, obj):
        if isinstance(obj, BillingCheckout):
            self.checkouts.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _CheckoutGateway:
    def __init__(self) -> None:
        self.pix_creates = 0
        self.card_creates = 0
        self.pix_cancels = 0

    async def find_customer(self, **kwargs):
        return "cus_test"

    async def create_customer(self, **kwargs):
        raise AssertionError("Existing test customer should be reused")

    async def create_pix_authorization(self, payload):
        self.pix_creates += 1
        return SimpleNamespace(
            authorization_id=f"auth_test_{self.pix_creates}",
            subscription_id=f"sub_test_{self.pix_creates}",
            payload=f"pix-payload-{self.pix_creates}",
            conciliation_identifier=f"conciliation-{self.pix_creates}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def cancel_pix_authorization(self, authorization_id):
        assert authorization_id.startswith("auth_test_")
        self.pix_cancels += 1

    async def create_checkout(self, payload):
        self.card_creates += 1
        return SimpleNamespace(
            checkout_id=f"card_checkout_{self.card_creates}",
            checkout_url=(
                "https://sandbox.asaas.com/checkoutSession/show/"
                f"card_checkout_{self.card_creates}"
            ),
        )


def _checkout_request(payment_method: str = "pix_automatic") -> CheckoutCreateRequest:
    values = {
        "plan": "basic",
        "cycle": "monthly",
        "payment_method": payment_method,
        "return_origin": "https://alovia.netlify.app",
    }
    if payment_method == "pix_automatic":
        values.update(
            payer_name="Empresa Teste",
            payer_cpf_cnpj="12345678000195",
        )
    return CheckoutCreateRequest(**values)


@pytest.mark.asyncio
async def test_pix_refresh_with_new_idempotency_key_reuses_authorization(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()

    first = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )
    refreshed = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert refreshed.checkout_id == first.checkout_id
    assert refreshed.pix_authorization_id == first.pix_authorization_id
    assert refreshed.pix_payload == first.pix_payload
    assert refreshed.pix_expires_at == first.pix_expires_at
    assert gateway.pix_creates == 1
    assert db.locks == 2


@pytest.mark.asyncio
async def test_card_refresh_with_new_idempotency_key_reuses_active_checkout(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()

    first = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )
    refreshed = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert refreshed.checkout_id == first.checkout_id
    assert refreshed.checkout_url == first.checkout_url
    assert gateway.card_creates == 1
    assert db.locks == 2


@pytest.mark.asyncio
async def test_expired_active_card_checkout_does_not_block_new_attempt(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()

    first = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )
    db.checkouts[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    replacement = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert db.checkouts[0].status == "expired"
    assert replacement.checkout_id != first.checkout_id
    assert gateway.card_creates == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_is_isolated_per_business(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    shared_key = uuid4()
    business_a = uuid4()
    business_b = uuid4()

    checkout_a = await service.create_checkout(
        business_id=business_a,
        payer_email="a@example.com",
        idempotency_key=shared_key,
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )
    checkout_b = await service.create_checkout(
        business_id=business_b,
        payer_email="b@example.com",
        idempotency_key=shared_key,
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert checkout_a.checkout_id != checkout_b.checkout_id
    assert gateway.card_creates == 2


@pytest.mark.asyncio
async def test_checkout_lock_and_blocking_state_are_isolated_per_business(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_a = uuid4()
    business_b = uuid4()

    checkout_a = await service.create_checkout(
        business_id=business_a,
        payer_email="a@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )
    checkout_b = await service.create_checkout(
        business_id=business_b,
        payer_email="b@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert checkout_a.checkout_id != checkout_b.checkout_id
    assert gateway.card_creates == 2
    assert len(db.lock_keys) == 2
    assert db.lock_keys[0] != db.lock_keys[1]
    assert str(business_a) in db.lock_keys[0]
    assert str(business_b) in db.lock_keys[1]


@pytest.mark.asyncio
async def test_active_pix_blocks_card_until_canceled(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()
    pix = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )

    with pytest.raises(HTTPException) as error:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request("credit_card"),
            allowed_origins=("https://alovia.netlify.app",),
        )
    assert error.value.status_code == 409
    assert gateway.card_creates == 0

    first_cancel = await service.cancel_checkout(
        business_id=business_id,
        checkout_id=pix.checkout_id,
    )
    second_cancel = await service.cancel_checkout(
        business_id=business_id,
        checkout_id=pix.checkout_id,
    )
    card = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert first_cancel.status == "canceled"
    assert second_cancel.status == "canceled"
    assert gateway.pix_cancels == 1
    assert card.payment_method == "credit_card"
    assert gateway.card_creates == 1


@pytest.mark.asyncio
async def test_active_card_blocks_pix_before_provider_creation(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()

    await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request("credit_card"),
        allowed_origins=("https://alovia.netlify.app",),
    )

    with pytest.raises(HTTPException) as error:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request(),
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "A card checkout is already active"
    assert gateway.pix_creates == 0
    assert gateway.card_creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_method", ["pix_automatic", "credit_card"])
async def test_active_commercial_subscription_blocks_new_acquisition(
    monkeypatch, payment_method
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    db.subscription = SimpleNamespace(status="active")
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)

    with pytest.raises(HTTPException) as error:
        await service.create_checkout(
            business_id=uuid4(),
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request(payment_method),
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Commercial subscription is already active"
    assert gateway.pix_creates == 0
    assert gateway.card_creates == 0


@pytest.mark.asyncio
async def test_locally_expired_pix_requires_provider_cancellation_before_retry(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()
    first = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )
    db.checkouts[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.checkouts[0].pix_qr_expires_at = db.checkouts[0].expires_at

    status = await service.checkout_status(
        business_id=business_id,
        checkout_id=first.checkout_id,
    )
    assert status.status == "active"

    with pytest.raises(HTTPException) as blocked:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request(),
            allowed_origins=("https://alovia.netlify.app",),
        )
    assert blocked.value.status_code == 409
    assert gateway.pix_creates == 1

    canceled = await service.cancel_checkout(
        business_id=business_id,
        checkout_id=first.checkout_id,
    )
    assert canceled.status == "canceled"
    replacement = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )
    assert replacement.checkout_id != first.checkout_id
    assert gateway.pix_cancels == 1
    assert gateway.pix_creates == 2


@pytest.mark.asyncio
async def test_uncertain_pix_cancellation_keeps_other_methods_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()

    async def fail_cancel(authorization_id):
        raise AsaasGatewayError("provider unavailable")

    gateway.cancel_pix_authorization = fail_cancel
    service = BillingService(db, gateway)
    business_id = uuid4()
    pix = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )

    with pytest.raises(HTTPException) as cancellation:
        await service.cancel_checkout(
            business_id=business_id,
            checkout_id=pix.checkout_id,
        )
    assert cancellation.value.status_code == 502

    with pytest.raises(HTTPException) as card:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request("credit_card"),
            allowed_origins=("https://alovia.netlify.app",),
        )
    assert card.value.status_code == 409
    assert gateway.card_creates == 0


@pytest.mark.asyncio
async def test_active_pix_rejects_a_different_commercial_cycle(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()
    await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )
    different_cycle = _checkout_request().model_copy(update={"cycle": "quarterly"})

    with pytest.raises(HTTPException) as error:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=different_cycle,
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert error.value.status_code == 409
    assert gateway.pix_creates == 1


@pytest.mark.asyncio
async def test_pix_cancellation_is_tenant_scoped_and_rejects_received_payment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()
    created = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )

    with pytest.raises(HTTPException) as cross_tenant:
        await service.cancel_checkout(
            business_id=uuid4(),
            checkout_id=created.checkout_id,
        )
    assert cross_tenant.value.status_code == 404
    assert gateway.pix_cancels == 0

    checkout = db.checkouts[0]
    checkout.paid_at = datetime.now(UTC)
    with pytest.raises(HTTPException) as payment_received:
        await service.cancel_checkout(
            business_id=business_id,
            checkout_id=created.checkout_id,
        )
    assert payment_received.value.status_code == 409
    assert gateway.pix_cancels == 0


def _pix_checkout() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        payment_method="pix_automatic",
        provider_environment="sandbox",
        provider_authorization_id="auth_pix_payment",
        provider_subscription_id="sub_pix_payment",
        provider_customer_id="cus_pix_payment",
        provider_checkout_id=None,
        checkout_url=None,
        pix_qr_payload="pix-payload",
        pix_qr_expires_at=datetime.now(UTC) + timedelta(hours=1),
        pix_conciliation_identifier="conciliation",
        plan_code="basic",
        billing_cycle="monthly",
        amount_cents=500,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        paid_at=None,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_pix_payment_received_records_financial_fact_without_access(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_payment_event(
        "PAYMENT_RECEIVED",
        {
            "id": "pay_pix_initial",
            "pixAutomaticAuthorizationId": checkout.provider_authorization_id,
            "customer": checkout.provider_customer_id,
            "value": 5.0,
        },
    )

    assert checkout.paid_at is not None
    assert checkout.status == "active"
    assert db.subscription is None
    assert db.locks == 1
    status = await service.checkout_status(
        business_id=checkout.business_id,
        checkout_id=checkout.id,
    )
    assert status.initial_payment_received is True


@pytest.mark.asyncio
async def test_pix_payment_received_before_cancellation_blocks_cancellation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)

    await service.apply_payment_event(
        "PAYMENT_RECEIVED",
        {
            "pixAutomaticAuthorizationId": checkout.provider_authorization_id,
            "value": 5.0,
        },
    )

    with pytest.raises(HTTPException) as cancellation:
        await service.cancel_checkout(
            business_id=checkout.business_id,
            checkout_id=checkout.id,
        )

    assert cancellation.value.status_code == 409
    assert checkout.paid_at is not None
    assert gateway.pix_cancels == 0
    assert db.locks == 2


@pytest.mark.asyncio
async def test_pix_payment_received_then_refused_does_not_create_or_retry(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_payment_event(
        "PAYMENT_CONFIRMED",
        {
            "pixAutomaticAuthorizationId": checkout.provider_authorization_id,
            "value": "5.00",
        },
    )
    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED",
        {"id": checkout.provider_authorization_id},
    )

    assert checkout.status == "failed"
    assert checkout.paid_at is not None
    assert db.subscription is None
    assert db.locks == 2
    status = await service.checkout_status(
        business_id=checkout.business_id,
        checkout_id=checkout.id,
    )
    assert status.initial_payment_received is True


@pytest.mark.asyncio
async def test_pix_paid_then_refused_reopens_same_checkout_without_new_charge(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    db = _CheckoutCreateDb()
    gateway = _CheckoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()
    created = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )
    checkout = db.checkouts[0]
    checkout.paid_at = datetime.now(UTC)
    checkout.status = "failed"

    reopened = await service.create_checkout(
        business_id=business_id,
        payer_email="teste@example.com",
        idempotency_key=uuid4(),
        payload=_checkout_request(),
        allowed_origins=("https://alovia.netlify.app",),
    )

    assert reopened.checkout_id == created.checkout_id
    assert reopened.initial_payment_received is True
    assert gateway.pix_creates == 1

    with pytest.raises(HTTPException) as card_attempt:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request("credit_card"),
            allowed_origins=("https://alovia.netlify.app",),
        )
    assert card_attempt.value.status_code == 409
    assert gateway.card_creates == 0


@pytest.mark.asyncio
async def test_pix_refused_without_payment_allows_retry_state(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED",
        {"id": checkout.provider_authorization_id},
    )
    status = await service.checkout_status(
        business_id=checkout.business_id,
        checkout_id=checkout.id,
    )

    assert checkout.status == "failed"
    assert status.initial_payment_received is False


@pytest.mark.asyncio
async def test_pix_payment_then_duplicate_activation_creates_one_subscription(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())
    event = {
        "id": checkout.provider_authorization_id,
        "customerId": checkout.provider_customer_id,
        "value": 5.0,
        "subscriptionId": checkout.provider_subscription_id,
    }

    await service.apply_payment_event(
        "PAYMENT_RECEIVED",
        {
            "pixAutomaticAuthorizationId": checkout.provider_authorization_id,
            "value": 5.0,
        },
    )
    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED", event
    )
    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED", event
    )

    assert checkout.status == "paid"
    assert checkout.paid_at is not None
    assert db.subscription is not None
    assert db.subscription.status == "active"
    assert db.subscription_adds == 1


@pytest.mark.asyncio
async def test_different_pix_authorization_cannot_create_second_active_subscription(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    existing = SimpleNamespace(
        id=uuid4(),
        checkout_id=uuid4(),
        business_id=checkout.business_id,
        provider_subscription_id="sub_existing",
        provider_authorization_id="auth_existing",
        provider_customer_id="cus_existing",
        provider_environment="sandbox",
        payment_method="pix_automatic",
        plan_code="basic",
        billing_cycle="monthly",
        status="active",
        access_until=datetime.now(UTC) + timedelta(days=30),
        created_at=datetime.now(UTC),
    )
    db.subscription = existing
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED",
        {
            "id": checkout.provider_authorization_id,
            "customerId": checkout.provider_customer_id,
            "value": 5.0,
            "subscriptionId": checkout.provider_subscription_id,
        },
    )

    assert checkout.status == "paid"
    assert db.subscription is existing
    assert db.subscription_adds == 0


@pytest.mark.asyncio
async def test_card_activation_cannot_create_second_active_subscription(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _card_checkout()
    checkout.status = "paid"
    checkout.provider_subscription_id = "sub_new_card"
    db = _BillingStateDb(checkout)
    existing = SimpleNamespace(
        id=uuid4(),
        checkout_id=uuid4(),
        business_id=checkout.business_id,
        provider_subscription_id="sub_existing_card",
        provider_authorization_id=None,
        provider_customer_id="cus_existing",
        provider_environment="sandbox",
        payment_method="credit_card",
        plan_code="basic",
        billing_cycle="monthly",
        status="active",
        access_until=datetime.now(UTC) + timedelta(days=30),
        created_at=datetime.now(UTC),
    )
    db.subscription = existing
    service = BillingService(db, _GatewayMustNotBeCalled())

    activated = await service._activate_credit_card_subscription_if_ready(checkout)

    assert activated is None
    assert db.subscription is existing
    assert db.subscription_adds == 0


@pytest.mark.asyncio
async def test_pix_payment_correlation_is_isolated_by_provider_environment(
    monkeypatch,
) -> None:
    checkout = _pix_checkout()
    checkout.provider_environment = "production"

    class EnvironmentDb:
        def __init__(self):
            self.commits = 0

        async def scalar(self, statement):
            sql = str(statement)
            if "FROM billing_checkouts" not in sql:
                return None
            values = set(statement.compile().params.values())
            return checkout if checkout.provider_environment in values else None

        async def get(self, model, identifier):
            if model is BillingCheckout and identifier == checkout.id:
                return checkout
            return None

        async def commit(self):
            self.commits += 1

    db = EnvironmentDb()
    payload = {
        "pixAutomaticAuthorizationId": checkout.provider_authorization_id,
        "value": 5.0,
    }

    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    await BillingService(db, _GatewayMustNotBeCalled()).apply_payment_event(
        "PAYMENT_RECEIVED", payload
    )
    assert checkout.paid_at is None

    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    await BillingService(db, _GatewayMustNotBeCalled()).apply_payment_event(
        "PAYMENT_RECEIVED", payload
    )
    assert checkout.paid_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_field", ["subscription", "contractId"])
async def test_pix_initial_payment_supports_sanitized_provider_correlation_fallbacks(
    monkeypatch,
    reference_field,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())
    reference = (
        checkout.provider_subscription_id
        if reference_field == "subscription"
        else checkout.id.hex
    )

    matched = await service._capture_pix_initial_payment(
        "PAYMENT_CONFIRMED",
        {reference_field: reference, "value": 5.0},
    )

    assert matched is checkout
    assert checkout.paid_at is not None


@pytest.mark.asyncio
async def test_pix_expiry_ignores_pre_activation_subscription_id_but_preserves_payment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    checkout.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db = _BillingStateDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    visually_expired = await service.checkout_status(
        business_id=checkout.business_id,
        checkout_id=checkout.id,
    )
    assert visually_expired.status == "active"
    assert visually_expired.expires_at <= datetime.now(UTC)

    checkout.status = "active"
    checkout.paid_at = datetime.now(UTC)
    paid = await service.checkout_status(
        business_id=checkout.business_id,
        checkout_id=checkout.id,
    )
    assert paid.status == "active"
    assert paid.initial_payment_received is True


@pytest.mark.asyncio
async def test_cancel_pix_gateway_uses_official_delete_contract() -> None:
    gateway = AsaasGateway(
        settings(
            ASAAS_API_KEY=SecretStr("$aact_hmlg_example")
        ).require_asaas_configuration()
    )
    calls = []

    async def fake_json_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {}

    gateway._json_request = fake_json_request  # type: ignore[method-assign]
    await gateway.cancel_pix_authorization("auth_valid_123")

    assert calls == [
        (
            "DELETE",
            "/pix/automatic/authorizations/auth_valid_123",
            {"allow_empty_response": True, "allow_not_found": True},
        )
    ]

@pytest.mark.asyncio
async def test_pix_authorization_timeout_stays_creating_and_blocks_second_attempt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")

    class TimeoutGateway(_CheckoutGateway):
        async def create_pix_authorization(self, payload):
            self.pix_creates += 1
            raise AsaasGatewayTimeoutError("provider timeout")

    db = _CheckoutCreateDb()
    gateway = TimeoutGateway()
    service = BillingService(db, gateway)
    business_id = uuid4()

    with pytest.raises(HTTPException) as first:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request(),
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert first.value.status_code == 502
    assert first.value.detail == "Pix Automatic authorization is being reconciled"
    assert len(db.checkouts) == 1
    assert db.checkouts[0].status == "creating"
    assert gateway.pix_creates == 1

    with pytest.raises(HTTPException) as second:
        await service.create_checkout(
            business_id=business_id,
            payer_email="teste@example.com",
            idempotency_key=uuid4(),
            payload=_checkout_request(),
            allowed_origins=("https://alovia.netlify.app",),
        )

    assert second.value.status_code == 409
    assert gateway.pix_creates == 1


@pytest.mark.asyncio
async def test_pix_authorization_webhook_recovers_timeout_checkout_by_contract_id(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    checkout.status = "creating"
    checkout.provider_authorization_id = None
    checkout.provider_subscription_id = None
    checkout.provider_customer_id = None
    checkout.pix_qr_payload = None
    checkout.pix_conciliation_identifier = None
    checkout.pix_qr_expires_at = None

    class ContractRecoveryDb(_BillingStateDb):
        async def scalar(self, statement):
            if "billing_checkouts.provider_authorization_id =" in str(statement):
                return None
            return await super().scalar(statement)

    db = ContractRecoveryDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CREATED",
        {
            "id": "auth_recovered",
            "contractId": checkout.id.hex,
            "customerId": "cus_recovered",
            "subscriptionId": "sub_recovered",
            "payload": "pix-recovered",
            "immediateQrCode": {
                "conciliationIdentifier": "conciliation-recovered",
                "expirationDate": "2026-10-16 18:00:00",
            },
        },
    )

    assert checkout.status == "active"
    assert checkout.provider_authorization_id == "auth_recovered"
    assert checkout.provider_subscription_id == "sub_recovered"
    assert checkout.provider_customer_id == "cus_recovered"
    assert checkout.pix_qr_payload == "pix-recovered"
    assert checkout.pix_conciliation_identifier == "conciliation-recovered"
    assert checkout.pix_qr_expires_at is not None
    assert db.locks == 1


@pytest.mark.asyncio
async def test_pix_authorization_webhook_ignores_invalid_contract_id(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    checkout = _pix_checkout()
    checkout.status = "creating"
    checkout.provider_authorization_id = None

    class MissingAuthorizationDb(_BillingStateDb):
        async def scalar(self, statement):
            if "billing_checkouts.provider_authorization_id =" in str(statement):
                return None
            return await super().scalar(statement)

    db = MissingAuthorizationDb(checkout)
    service = BillingService(db, _GatewayMustNotBeCalled())

    await service.apply_pix_authorization_event(
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CREATED",
        {"id": "auth_unknown", "contractId": "not-a-valid-contract"},
    )

    assert checkout.provider_authorization_id is None
    assert checkout.status == "creating"
    assert db.locks == 0


@pytest.mark.asyncio
async def test_native_card_submit_is_serialized_and_provider_called_once(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_TEST_AMOUNT_CENTS", "500")
    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")

    checkout = SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        payment_method="credit_card",
        provider_environment="sandbox",
        provider_checkout_id=None,
        provider_subscription_id=None,
        provider_customer_id=None,
        checkout_url=None,
        plan_code="basic",
        billing_cycle="monthly",
        amount_cents=500,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        paid_at=None,
        created_at=datetime.now(UTC),
    )
    db = _BillingStateDb(checkout)

    class CardGateway:
        def __init__(self):
            self.subscription_creates = 0

        async def find_customer(self, **kwargs):
            return "cus_card"

        async def create_customer(self, **kwargs):
            raise AssertionError("Existing customer should be reused")

        async def create_credit_card_subscription(self, payload):
            self.subscription_creates += 1
            return SimpleNamespace(subscription_id="sub_card_once")

        async def payments_for_subscription(self, subscription_id):
            return []

        async def find_subscription(self, **kwargs):
            return None

    gateway = CardGateway()
    service = BillingService(db, gateway)
    request = CreditCardCheckoutRequest(
        payer_name="Teste Alovia",
        payer_cpf_cnpj="12345678909",
        payer_postal_code="12235740",
        payer_address_number="160",
        payer_phone="11999999999",
        card_holder_name="TESTE ALOVIA",
        card_number="4444444444444444",
        card_expiry_month="10",
        card_expiry_year="2030",
        card_ccv="123",
    )

    first = await service.pay_credit_card(
        business_id=checkout.business_id,
        payer_email="teste@example.com",
        checkout_id=checkout.id,
        payload=request,
        remote_ip="203.0.113.10",
    )
    second = await service.pay_credit_card(
        business_id=checkout.business_id,
        payer_email="teste@example.com",
        checkout_id=checkout.id,
        payload=request,
        remote_ip="203.0.113.10",
    )

    assert first.status == "active"
    assert second.status == "active"
    assert gateway.subscription_creates == 1
    assert db.locks == 2


@pytest.mark.asyncio
async def test_active_subscription_blocks_native_card_provider_call(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("BILLING_NATIVE_CARD_CHECKOUT_ENABLED", "true")

    checkout = SimpleNamespace(
        id=uuid4(),
        business_id=uuid4(),
        payment_method="credit_card",
        provider_environment="sandbox",
        provider_checkout_id=None,
        provider_subscription_id=None,
        provider_customer_id=None,
        checkout_url=None,
        plan_code="basic",
        billing_cycle="monthly",
        amount_cents=500,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        paid_at=None,
        created_at=datetime.now(UTC),
    )
    db = _BillingStateDb(checkout)
    db.subscription = SimpleNamespace(
        id=uuid4(),
        checkout_id=uuid4(),
        business_id=checkout.business_id,
        provider_environment="sandbox",
        status="active",
        created_at=datetime.now(UTC),
    )

    class Gateway:
        def __getattr__(self, name):
            raise AssertionError(f"Provider must not be called: {name}")

    request = CreditCardCheckoutRequest(
        payer_name="Teste Alovia",
        payer_cpf_cnpj="12345678909",
        payer_postal_code="12235740",
        payer_address_number="160",
        payer_phone="11999999999",
        card_holder_name="TESTE ALOVIA",
        card_number="4444444444444444",
        card_expiry_month="10",
        card_expiry_year="2030",
        card_ccv="123",
    )

    with pytest.raises(HTTPException) as error:
        await BillingService(db, Gateway()).pay_credit_card(
            business_id=checkout.business_id,
            payer_email="teste@example.com",
            checkout_id=checkout.id,
            payload=request,
            remote_ip="203.0.113.10",
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Commercial subscription is already active"
    assert db.locks == 1


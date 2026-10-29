from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.billing.service as billing_service_module
from app.billing.asaas import AsaasGatewayTimeoutError
from app.billing.service import BillingService
from app.models import BillingCheckout, BusinessAccess, CommercialSubscription


class _StatusDb:
    def __init__(self, checkout: BillingCheckout, first_paid_checkout_id):
        self.checkout = checkout
        self.first_paid_checkout_id = first_paid_checkout_id

    async def get(self, model, identifier):
        if model is BillingCheckout and identifier == self.checkout.id:
            return self.checkout
        return None

    async def scalar(self, statement):
        return self.first_paid_checkout_id


def _checkout(*, business_id, paid_at: datetime) -> BillingCheckout:
    return BillingCheckout(
        id=uuid4(),
        business_id=business_id,
        idempotency_key=uuid4(),
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        provider_environment="sandbox",
        amount_cents=19_700,
        status="paid",
        paid_at=paid_at,
        provider_pix_qr_code_id=f"qr_{uuid4().hex}",
    )


def _subscription(*, business_id, checkout_id) -> CommercialSubscription:
    return CommercialSubscription(
        id=uuid4(),
        business_id=business_id,
        checkout_id=checkout_id,
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        provider_environment="sandbox",
        status="active",
        provider_pix_qr_code_id=f"qr_{uuid4().hex}",
        access_until=datetime.now(UTC) + timedelta(days=30),
        refund_status="none",
    )


@pytest.mark.asyncio
async def test_first_paid_checkout_gets_exact_seven_day_guarantee(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    paid_at = datetime.now(UTC) - timedelta(days=1)
    checkout = _checkout(business_id=business_id, paid_at=paid_at)
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    service = BillingService(_StatusDb(checkout, checkout.id), object())

    response = await service._subscription_status_response(subscription)

    assert response.guarantee_until == paid_at + timedelta(days=7)
    assert response.guarantee_eligible is True
    assert response.refund_status == "none"


@pytest.mark.asyncio
async def test_guarantee_boundary_is_inclusive_and_expires_one_microsecond_later(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    paid_at = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
    checkout = _checkout(business_id=business_id, paid_at=paid_at)
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    service = BillingService(_StatusDb(checkout, checkout.id), object())

    class FrozenDateTime(datetime):
        current = paid_at + timedelta(days=7)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(billing_service_module, "datetime", FrozenDateTime)

    at_boundary = await service._subscription_status_response(subscription)
    assert at_boundary.guarantee_eligible is True

    FrozenDateTime.current += timedelta(microseconds=1)
    after_boundary = await service._subscription_status_response(subscription)
    assert after_boundary.guarantee_eligible is False


@pytest.mark.asyncio
async def test_repurchase_does_not_receive_a_new_guarantee(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(hours=1),
    )
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    service = BillingService(_StatusDb(checkout, uuid4()), object())

    response = await service._subscription_status_response(subscription)

    assert response.guarantee_until is not None
    assert response.guarantee_eligible is False


def test_initial_payment_id_is_captured_once() -> None:
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC),
    )
    checkout.provider_payment_id = None

    BillingService._capture_initial_payment_id(
        checkout,
        "PAYMENT_CONFIRMED",
        {"id": "pay_initial"},
    )
    BillingService._capture_initial_payment_id(
        checkout,
        "PAYMENT_RECEIVED",
        {"id": "pay_different"},
    )

    assert checkout.provider_payment_id == "pay_initial"


def test_non_final_payment_event_does_not_set_initial_payment_id() -> None:
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC),
    )
    checkout.provider_payment_id = None

    BillingService._capture_initial_payment_id(
        checkout,
        "PAYMENT_CREATED",
        {"id": "pay_created"},
    )

    assert checkout.provider_payment_id is None


class _RequestDb:
    def __init__(self, checkout, scalar_results):
        self.checkout = checkout
        self.scalar_results = list(scalar_results)
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, statement):
        return self.scalar_results.pop(0)

    async def get(self, model, identifier):
        if model is BillingCheckout and identifier == self.checkout.id:
            return self.checkout
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _EmptyPixGateway:
    async def payments_for_static_pix(self, qr_code_id):
        return []


class _RefundGateway:
    def __init__(
        self,
        *,
        fail_refund: bool = False,
        payment_payload=None,
        fail_cancel_calls: int = 0,
    ):
        self.fail_refund = fail_refund
        self.payment_payload = payment_payload
        self.fail_cancel_calls = fail_cancel_calls
        self.refund_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.authorization_cancel_calls: list[str] = []
        self.payment_detail_calls: list[str] = []
        self.order: list[str] = []

    async def refund_payment(self, payment_id):
        self.refund_calls.append(payment_id)
        self.order.append("refund")
        if self.fail_refund:
            raise AsaasGatewayTimeoutError("timeout")

    async def payment_details(self, payment_id):
        self.payment_detail_calls.append(payment_id)
        if self.payment_payload is None:
            raise AsaasGatewayTimeoutError("lookup timeout")
        return self.payment_payload

    async def cancel_subscription(self, subscription_id):
        self.cancel_calls.append(subscription_id)
        self.order.append("cancel_subscription")
        if self.fail_cancel_calls > 0:
            self.fail_cancel_calls -= 1
            raise AsaasGatewayTimeoutError("timeout")

    async def cancel_pix_authorization(self, authorization_id):
        self.authorization_cancel_calls.append(authorization_id)
        self.order.append("cancel_pix_authorization")


@pytest.mark.asyncio
async def test_provider_awaiting_critical_authorization_is_reported(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.status = "canceled"
    subscription.refund_status = "reconcile_required"
    db = _RequestDb(checkout, [checkout.id])
    gateway = _RefundGateway(
        payment_payload={
            "refunds": [{
                "status": "AWAITING_CRITICAL_ACTION_AUTHORIZATION",
                "value": 197.0,
            }]
        }
    )
    service = BillingService(db, gateway)

    response = await service._subscription_status_response(subscription)

    assert response.refund_status == "requested"
    assert response.refund_provider_status == "awaiting_authorization"
    assert subscription.refunded_at is None
    assert gateway.payment_detail_calls == ["pay_initial"]


@pytest.mark.asyncio
async def test_provider_pending_refund_is_reported_without_declaring_completion(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.status = "canceled"
    subscription.refund_status = "requested"
    db = _RequestDb(checkout, [checkout.id])
    gateway = _RefundGateway(
        payment_payload={"refunds": [{"status": "PENDING", "value": 197.0}]}
    )
    service = BillingService(db, gateway)

    response = await service._subscription_status_response(subscription)

    assert response.refund_status == "requested"
    assert response.refund_provider_status == "pending"
    assert subscription.refunded_at is None
    assert gateway.payment_detail_calls == ["pay_initial"]


@pytest.mark.asyncio
async def test_provider_done_refund_reconciles_local_access(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.status = "canceled"
    subscription.refund_status = "requested"
    db = _RequestDb(checkout, [checkout.id])
    gateway = _RefundGateway(
        payment_payload={"refunds": [{"status": "DONE", "value": 197.0}]}
    )
    service = BillingService(db, gateway)

    response = await service._subscription_status_response(subscription)

    assert response.refund_status == "refunded"
    assert response.refund_provider_status == "done"
    assert subscription.status == "suspended"
    assert subscription.refunded_at is not None
    assert subscription.access_until <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_partial_provider_refund_requires_reconciliation(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.status = "canceled"
    subscription.refund_status = "requested"
    db = _RequestDb(checkout, [checkout.id])
    gateway = _RefundGateway(
        payment_payload={"refunds": [{"status": "DONE", "value": 50.0}]}
    )
    service = BillingService(db, gateway)

    response = await service._subscription_status_response(subscription)

    assert response.refund_status == "reconcile_required"
    assert response.refund_provider_status == "partial"
    assert subscription.refunded_at is None


@pytest.mark.asyncio
async def test_guarantee_refund_request_is_persisted_without_declaring_refund(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    db = _RequestDb(checkout, [subscription, checkout.id, checkout.id])
    gateway = _RefundGateway()
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    response = await service.request_guarantee_refund(business_id=business_id)

    assert response.refund_status == "requested"
    assert gateway.refund_calls == ["pay_initial"]
    assert subscription.refund_requested_at is not None
    assert response.refund_deadline_at == subscription.refund_requested_at + timedelta(hours=24)
    assert subscription.refunded_at is None
    assert subscription.status == "canceled"
    assert subscription.access_until > datetime.now(UTC)
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_refund_timeout_requires_reconciliation_and_never_claims_completion(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    db = _RequestDb(checkout, [subscription, checkout.id, checkout.id])
    gateway = _RefundGateway(fail_refund=True)
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    response = await service.request_guarantee_refund(business_id=business_id)

    assert gateway.refund_calls == ["pay_initial"]
    assert response.refund_status == "reconcile_required"
    assert subscription.refunded_at is None
    assert subscription.status == "canceled"
    assert subscription.access_until > datetime.now(UTC)


@pytest.mark.asyncio
async def test_refund_reconciliation_retries_failed_recurrence_cancel_then_submits_once(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.payment_method = "credit_card"
    checkout.provider_subscription_id = "sub_card"
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.payment_method = "credit_card"
    subscription.provider_pix_qr_code_id = None
    subscription.provider_subscription_id = "sub_card"
    db = _RequestDb(
        checkout,
        [
            subscription,
            checkout.id,
            checkout.id,
        ],
    )
    gateway = _RefundGateway(fail_cancel_calls=1)
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    first = await service.request_guarantee_refund(business_id=business_id)

    assert first.refund_status == "reconcile_required"
    assert subscription.status == "active"
    assert gateway.cancel_calls == ["sub_card"]
    assert gateway.refund_calls == []

    reconciled = await service.subscription_status(business_id=business_id)

    assert gateway.cancel_calls == ["sub_card", "sub_card"]
    assert gateway.refund_calls == ["pay_initial"]
    assert reconciled.refund_status == "requested"
    assert subscription.status == "canceled"


@pytest.mark.asyncio
async def test_refund_timeout_is_never_resubmitted_by_status_reconciliation(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.payment_method = "credit_card"
    checkout.provider_subscription_id = "sub_card"
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.payment_method = "credit_card"
    subscription.provider_pix_qr_code_id = None
    subscription.provider_subscription_id = "sub_card"
    db = _RequestDb(
        checkout,
        [
            subscription,
            checkout.id,
            subscription,
            checkout.id,
        ],
    )
    gateway = _RefundGateway(
        fail_refund=True,
        payment_payload={"refunds": []},
    )
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    first = await service.request_guarantee_refund(business_id=business_id)

    assert first.refund_status == "reconcile_required"
    assert subscription.status == "canceled"
    assert gateway.refund_calls == ["pay_initial"]

    await service.subscription_status(business_id=business_id)

    assert gateway.refund_calls == ["pay_initial"]


@pytest.mark.asyncio
async def test_card_cancellation_stops_provider_recurrence_and_preserves_access(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=10),
    )
    checkout.payment_method = "credit_card"
    checkout.provider_subscription_id = "sub_card"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.payment_method = "credit_card"
    subscription.provider_pix_qr_code_id = None
    subscription.provider_subscription_id = "sub_card"
    previous_access_until = subscription.access_until
    db = _RequestDb(checkout, [subscription, checkout.id])
    gateway = _RefundGateway()
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    response = await service.request_subscription_cancellation(business_id=business_id)

    assert gateway.cancel_calls == ["sub_card"]
    assert response.status == "canceled"
    assert response.access_until == previous_access_until
    assert subscription.canceled_at is not None


@pytest.mark.asyncio
async def test_card_cancellation_can_be_retried_after_uncertain_provider_failure(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=10),
    )
    checkout.payment_method = "credit_card"
    checkout.provider_subscription_id = "sub_card"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.payment_method = "credit_card"
    subscription.provider_pix_qr_code_id = None
    subscription.provider_subscription_id = "sub_card"
    previous_access_until = subscription.access_until
    db = _RequestDb(
        checkout,
        [
            subscription,
            subscription,
            checkout.id,
        ],
    )
    gateway = _RefundGateway(fail_cancel_calls=1)
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    with pytest.raises(HTTPException) as first:
        await service.request_subscription_cancellation(business_id=business_id)

    assert first.value.status_code == 502
    assert first.value.detail == "Cancellation is pending confirmation; retry safely"
    assert subscription.cancellation_requested_at is not None
    assert subscription.status == "active"
    assert gateway.cancel_calls == ["sub_card"]

    response = await service.request_subscription_cancellation(business_id=business_id)

    assert gateway.cancel_calls == ["sub_card", "sub_card"]
    assert response.status == "canceled"
    assert response.access_until == previous_access_until
    assert subscription.canceled_at is not None


@pytest.mark.asyncio
async def test_card_guarantee_cancels_recurrence_before_submitting_refund(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.payment_method = "credit_card"
    checkout.provider_subscription_id = "sub_card"
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.payment_method = "credit_card"
    subscription.provider_pix_qr_code_id = None
    subscription.provider_subscription_id = "sub_card"
    db = _RequestDb(checkout, [subscription, checkout.id, checkout.id])
    gateway = _RefundGateway()
    service = BillingService(db, gateway)
    service._lock_business_checkout = AsyncMock()

    response = await service.request_guarantee_refund(business_id=business_id)

    assert gateway.order == ["cancel_subscription", "refund"]
    assert gateway.refund_calls == ["pay_initial"]
    assert response.refund_status == "requested"
    assert response.status == "canceled"
    assert subscription.refunded_at is None


@pytest.mark.asyncio
async def test_unresolved_legacy_payment_preserves_in_window_refund_request(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = None
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    db = _RequestDb(checkout, [subscription, checkout.id, checkout.id])
    service = BillingService(db, _EmptyPixGateway())
    service._lock_business_checkout = AsyncMock()

    response = await service.request_guarantee_refund(business_id=business_id)

    assert response.refund_status == "reconcile_required"
    assert subscription.refund_requested_at is not None
    assert subscription.refunded_at is None
    assert subscription.status == "active"
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_common_pix_cancellation_preserves_already_paid_access(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=10),
    )
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    previous_access_until = subscription.access_until
    db = _RequestDb(checkout, [subscription, checkout.id])
    service = BillingService(db, object())
    service._lock_business_checkout = AsyncMock()

    response = await service.request_subscription_cancellation(business_id=business_id)

    assert response.status == "canceled"
    assert subscription.canceled_at is not None
    assert response.access_until == previous_access_until
    assert response.refund_status == "none"


class _RefundEventDb:
    def __init__(self, checkout, subscription):
        self.checkout = checkout
        self.subscription = subscription
        self.scalar_results = [checkout, subscription]
        self.commits = 0

    async def scalar(self, statement):
        return self.scalar_results.pop(0)

    async def get(self, model, identifier):
        if model is BillingCheckout and identifier == self.checkout.id:
            return self.checkout
        return None

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        raise AssertionError("Refund lifecycle should not create an action event")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "expected_refund_status"),
    [
        ("PAYMENT_REFUND_IN_PROGRESS", "requested"),
        ("PAYMENT_PARTIALLY_REFUNDED", "reconcile_required"),
        ("PAYMENT_REFUND_DENIED", "reconcile_required"),
    ],
)
async def test_nonfinal_refund_events_never_claim_full_refund(
    monkeypatch, event_type, expected_refund_status
) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    original_access_until = subscription.access_until
    db = _RefundEventDb(checkout, subscription)
    service = BillingService(db, object())

    await service.apply_payment_event(
        event_type,
        {"id": "pay_initial", "value": 197.0},
    )

    assert subscription.refund_status == expected_refund_status
    assert subscription.refunded_at is None
    assert subscription.status == "active"
    assert subscription.access_until == original_access_until


@pytest.mark.asyncio
async def test_full_refund_returns_production_business_to_free_access(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "production")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_environment = "production"
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    subscription.provider_environment = "production"
    access = BusinessAccess(
        business_id=business_id,
        access_mode="paid",
        admin_full_access=False,
        has_had_operational_access=True,
    )

    class ProductionRefundDb(_RefundEventDb):
        async def get(self, model, identifier):
            if model is BusinessAccess and identifier == business_id:
                return access
            return await super().get(model, identifier)

    db = ProductionRefundDb(checkout, subscription)
    service = BillingService(db, object())

    await service.apply_payment_event(
        "PAYMENT_REFUNDED",
        {"id": "pay_initial", "value": 197.0},
    )

    assert subscription.refund_status == "refunded"
    assert subscription.status == "suspended"
    assert access.access_mode == "free"
    assert access.has_had_operational_access is True


@pytest.mark.asyncio
async def test_full_refund_webhook_is_required_before_access_is_suspended(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    business_id = uuid4()
    checkout = _checkout(
        business_id=business_id,
        paid_at=datetime.now(UTC) - timedelta(days=1),
    )
    checkout.provider_payment_id = "pay_initial"
    subscription = _subscription(business_id=business_id, checkout_id=checkout.id)
    db = _RefundEventDb(checkout, subscription)
    service = BillingService(db, object())

    await service.apply_payment_event(
        "PAYMENT_REFUNDED",
        {"id": "pay_initial", "value": 197.0},
    )

    assert subscription.refund_status == "refunded"
    assert subscription.refunded_at is not None
    assert subscription.status == "suspended"
    assert subscription.access_until <= datetime.now(UTC)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.billing.service import BillingService
from app.models import BillingCheckout, CommercialSubscription


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
    service = BillingService(db, object())
    service._lock_business_checkout = AsyncMock()

    response = await service.request_guarantee_refund(business_id=business_id)

    assert response.refund_status == "requested"
    assert subscription.refund_requested_at is not None
    assert subscription.refunded_at is None
    assert subscription.status == "canceled"
    assert subscription.access_until > datetime.now(UTC)
    assert db.rollbacks == 0


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

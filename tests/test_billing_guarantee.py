from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

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

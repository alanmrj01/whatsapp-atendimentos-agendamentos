from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas import AsaasGateway, AsaasGatewayError
from app.billing.catalog import BillingCycle, cycle_months, get_offer
from app.billing.schemas import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CheckoutStatusResponse,
    SubscriptionStatusResponse,
)
from app.models import BillingCheckout, BusinessAccess, CommercialSubscription


class BillingService:
    def __init__(self, db: AsyncSession, gateway: AsaasGateway) -> None:
        self.db = db
        self.gateway = gateway

    async def create_checkout(
        self,
        *,
        business_id: UUID,
        idempotency_key: UUID,
        payload: CheckoutCreateRequest,
        allowed_origins: tuple[str, ...],
    ) -> CheckoutCreateResponse:
        if payload.return_origin not in allowed_origins:
            raise HTTPException(400, "Invalid return origin")
        offer = get_offer(payload.plan, payload.cycle)
        existing = await self.db.scalar(
            select(BillingCheckout).where(BillingCheckout.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if (
                existing.business_id != business_id
                or existing.plan_code != offer.plan
                or existing.billing_cycle != offer.cycle
            ):
                raise HTTPException(409, "Idempotency key already used")
            if existing.checkout_url and existing.provider_checkout_id:
                return self._checkout_response(existing)
            raise HTTPException(409, "Checkout is still being prepared")

        checkout = BillingCheckout(
            id=uuid4(),
            business_id=business_id,
            idempotency_key=idempotency_key,
            plan_code=offer.plan,
            billing_cycle=offer.cycle,
            amount_cents=offer.amount_cents,
            status="creating",
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
        )
        self.db.add(checkout)
        await self.db.commit()

        return_url = f"{payload.return_origin}/app/checkout/retorno"
        provider_payload = {
            "billingTypes": ["CREDIT_CARD"],
            "chargeTypes": ["RECURRENT"],
            "minutesToExpire": 60,
            "externalReference": f"alovia:{checkout.id}",
            "callback": {
                "successUrl": f"{return_url}?state=success&checkout={checkout.id}",
                "cancelUrl": f"{return_url}?state=canceled&checkout={checkout.id}",
                "expiredUrl": f"{return_url}?state=expired&checkout={checkout.id}",
            },
            "items": [
                {
                    "externalReference": f"alovia-plan:{offer.plan}:{offer.cycle}",
                    "name": f"ALOVIA {offer.plan_name}",
                    "description": f"Assinatura {offer.plan_name} - {offer.cycle}",
                    "quantity": 1,
                    "value": offer.amount_cents / 100,
                }
            ],
            "subscription": {
                "cycle": offer.asaas_cycle,
                "nextDueDate": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        try:
            created = await self.gateway.create_checkout(provider_payload)
        except AsaasGatewayError:
            checkout.status = "failed"
            await self.db.commit()
            raise HTTPException(502, "Payment checkout is temporarily unavailable") from None

        checkout.provider_checkout_id = created.checkout_id
        checkout.checkout_url = created.checkout_url
        checkout.status = "active"
        await self.db.commit()
        return self._checkout_response(checkout)

    async def checkout_status(
        self, *, business_id: UUID, checkout_id: UUID
    ) -> CheckoutStatusResponse:
        checkout = await self.db.get(BillingCheckout, checkout_id)
        if checkout is None or checkout.business_id != business_id:
            raise HTTPException(404, "Checkout not found")
        return CheckoutStatusResponse(
            checkout_id=checkout.id,
            status=checkout.status,  # type: ignore[arg-type]
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
        )

    async def subscription_status(self, *, business_id: UUID) -> SubscriptionStatusResponse:
        subscription = await self.db.scalar(
            select(CommercialSubscription)
            .where(CommercialSubscription.business_id == business_id)
            .order_by(CommercialSubscription.created_at.desc())
            .limit(1)
        )
        if subscription is None:
            return SubscriptionStatusResponse(status="none")
        return SubscriptionStatusResponse(
            status=subscription.status,  # type: ignore[arg-type]
            plan=subscription.plan_code,  # type: ignore[arg-type]
            cycle=subscription.billing_cycle,  # type: ignore[arg-type]
            access_until=subscription.access_until,
        )

    async def activate_paid_checkout(self, provider_checkout_id: str) -> None:
        checkout = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_checkout_id == provider_checkout_id
            )
        )
        if checkout is None:
            return
        if checkout.status == "paid":
            return

        payments = await self.gateway.payments_for_checkout(provider_checkout_id)
        payment = next(
            (
                row
                for row in payments
                if isinstance(row.get("subscription"), str)
                and row.get("subscription")
            ),
            None,
        )
        if payment is None:
            raise AsaasGatewayError("Checkout subscription reconciliation pending")

        provider_subscription_id = str(payment["subscription"])
        provider_customer_id = payment.get("customer")
        if provider_customer_id is not None:
            provider_customer_id = str(provider_customer_id)

        checkout.status = "paid"
        checkout.paid_at = datetime.now(UTC)
        checkout.provider_subscription_id = provider_subscription_id
        checkout.provider_customer_id = provider_customer_id

        subscription = await self.db.scalar(
            select(CommercialSubscription).where(
                CommercialSubscription.checkout_id == checkout.id
            )
        )
        if subscription is None:
            subscription = CommercialSubscription(
                id=uuid4(),
                business_id=checkout.business_id,
                checkout_id=checkout.id,
                plan_code=checkout.plan_code,
                billing_cycle=checkout.billing_cycle,
                status="active",
                provider_subscription_id=provider_subscription_id,
                provider_customer_id=provider_customer_id,
                access_until=_cycle_end(datetime.now(UTC), checkout.billing_cycle),
            )
            self.db.add(subscription)
        else:
            subscription.status = "active"
            subscription.provider_subscription_id = provider_subscription_id
            subscription.provider_customer_id = provider_customer_id
            subscription.access_until = max(
                subscription.access_until,
                _cycle_end(datetime.now(UTC), checkout.billing_cycle),
            )
        await self._record_operational_history(checkout.business_id)
        await self.db.commit()

    async def apply_subscription_event(self, event_type: str, payload: dict) -> None:
        provider_id = payload.get("id")
        if not isinstance(provider_id, str):
            return
        subscription = await self.db.scalar(
            select(CommercialSubscription).where(
                CommercialSubscription.provider_subscription_id == provider_id
            )
        )
        if subscription is None:
            return
        if event_type in {"SUBSCRIPTION_INACTIVATED", "SUBSCRIPTION_DELETED"}:
            subscription.status = "canceled"
            subscription.canceled_at = datetime.now(UTC)
        elif event_type in {"SUBSCRIPTION_CREATED", "SUBSCRIPTION_UPDATED"}:
            provider_status = payload.get("status")
            if provider_status == "ACTIVE" and subscription.status != "suspended":
                subscription.status = "active"
        await self.db.commit()

    async def apply_payment_event(self, event_type: str, payload: dict) -> None:
        provider_subscription_id = payload.get("subscription")
        if not isinstance(provider_subscription_id, str) or not provider_subscription_id:
            return
        subscription = await self.db.scalar(
            select(CommercialSubscription).where(
                CommercialSubscription.provider_subscription_id == provider_subscription_id
            )
        )
        if subscription is None:
            return

        if event_type in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            due_date = _parse_due_date(payload.get("dueDate"))
            anchor = (
                datetime.combine(due_date, datetime.min.time(), tzinfo=UTC)
                if due_date is not None
                else datetime.now(UTC)
            )
            subscription.access_until = max(
                subscription.access_until,
                _cycle_end(anchor, subscription.billing_cycle),
            )
            subscription.status = "active"
            await self._record_operational_history(subscription.business_id)
        elif event_type == "PAYMENT_OVERDUE":
            subscription.status = "past_due"
        elif event_type in {
            "PAYMENT_REFUNDED",
            "PAYMENT_CHARGEBACK_REQUESTED",
            "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
        }:
            subscription.status = "suspended"
            subscription.access_until = min(subscription.access_until, datetime.now(UTC))
        await self.db.commit()

    async def _record_operational_history(self, business_id: UUID) -> None:
        access = await self.db.get(BusinessAccess, business_id)
        if access is None:
            self.db.add(
                BusinessAccess(
                    business_id=business_id,
                    access_mode="free",
                    has_had_operational_access=True,
                )
            )
        else:
            access.has_had_operational_access = True

    @staticmethod
    def _checkout_response(checkout: BillingCheckout) -> CheckoutCreateResponse:
        assert checkout.checkout_url is not None
        return CheckoutCreateResponse(
            checkout_id=checkout.id,
            checkout_url=checkout.checkout_url,
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
            amount_cents=checkout.amount_cents,
        )


def _parse_due_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _cycle_end(anchor: datetime, cycle: str) -> datetime:
    months = cycle_months(cycle)  # type: ignore[arg-type]
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)

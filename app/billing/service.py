from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

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
from app.models.billing import billing_provider_environment


BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


class BillingService:
    def __init__(self, db: AsyncSession, gateway: AsaasGateway) -> None:
        self.db = db
        self.gateway = gateway
        self.provider_environment = billing_provider_environment()

    async def create_checkout(
        self,
        *,
        business_id: UUID,
        payer_email: str,
        idempotency_key: UUID,
        payload: CheckoutCreateRequest,
        allowed_origins: tuple[str, ...],
    ) -> CheckoutCreateResponse:
        if payload.return_origin not in allowed_origins:
            raise HTTPException(400, "Invalid return origin")
        offer = get_offer(payload.plan, payload.cycle)
        existing = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.idempotency_key == idempotency_key,
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if existing is not None:
            if (
                existing.business_id != business_id
                or existing.plan_code != offer.plan
                or existing.billing_cycle != offer.cycle
                or existing.payment_method != payload.payment_method
            ):
                raise HTTPException(409, "Idempotency key already used")
            if existing.status in {"active", "paid"}:
                return self._checkout_response(existing)
            raise HTTPException(409, "Checkout is still being prepared")

        checkout = BillingCheckout(
            id=uuid4(),
            business_id=business_id,
            idempotency_key=idempotency_key,
            plan_code=offer.plan,
            billing_cycle=offer.cycle,
            payment_method=payload.payment_method,
            provider_environment=self.provider_environment,
            amount_cents=offer.amount_cents,
            status="creating",
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
        )
        self.db.add(checkout)
        await self.db.commit()

        try:
            if payload.payment_method == "pix_automatic":
                await self._prepare_pix_automatic(
                    checkout=checkout,
                    offer=offer,
                    payer_name=payload.payer_name or "",
                    payer_cpf_cnpj=payload.payer_cpf_cnpj or "",
                    payer_email=payer_email,
                )
            else:
                await self._prepare_credit_card(
                    checkout=checkout,
                    offer=offer,
                    return_origin=payload.return_origin,
                )
        except AsaasGatewayError:
            checkout.status = "failed"
            await self.db.commit()
            raise HTTPException(502, "Payment checkout is temporarily unavailable") from None

        checkout.status = "active"
        await self.db.commit()
        return self._checkout_response(checkout)

    async def _prepare_credit_card(self, *, checkout, offer, return_origin: str) -> None:
        return_url = f"{return_origin}/app/checkout/retorno"
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
        created = await self.gateway.create_checkout(provider_payload)
        checkout.provider_checkout_id = created.checkout_id
        checkout.checkout_url = created.checkout_url

    async def _prepare_pix_automatic(
        self,
        *,
        checkout: BillingCheckout,
        offer,
        payer_name: str,
        payer_cpf_cnpj: str,
        payer_email: str,
    ) -> None:
        external_reference = f"alovia:{checkout.business_id}"
        customer_id = await self.gateway.find_customer(
            external_reference=external_reference,
            cpf_cnpj=payer_cpf_cnpj,
        )
        if customer_id is None:
            customer_id = await self.gateway.create_customer(
                name=payer_name,
                cpf_cnpj=payer_cpf_cnpj,
                email=payer_email,
                external_reference=external_reference,
            )

        today = datetime.now(BRAZIL_TZ).date().isoformat()
        authorization = await self.gateway.create_pix_authorization(
            {
                "frequency": offer.asaas_pix_frequency,
                "contractId": checkout.id.hex,
                "startDate": today,
                "value": offer.amount_cents / 100,
                "description": f"ALOVIA {offer.plan_name}",
                "customerId": customer_id,
                "immediateQrCode": {},
                "paymentCreationMode": "SUBSCRIPTION",
                "retryPolicy": "ALLOW_THREE_IN_SEVEN_DAYS",
            }
        )
        checkout.provider_customer_id = customer_id
        checkout.provider_authorization_id = authorization.authorization_id
        checkout.pix_qr_payload = authorization.payload
        checkout.pix_conciliation_identifier = authorization.conciliation_identifier
        checkout.pix_qr_expires_at = authorization.expires_at
        checkout.expires_at = authorization.expires_at or checkout.expires_at

    async def checkout_status(
        self, *, business_id: UUID, checkout_id: UUID
    ) -> CheckoutStatusResponse:
        checkout = await self.db.get(BillingCheckout, checkout_id)
        if (
            checkout is None
            or checkout.business_id != business_id
            or checkout.provider_environment != self.provider_environment
        ):
            raise HTTPException(404, "Checkout not found")
        return CheckoutStatusResponse(
            checkout_id=checkout.id,
            status=checkout.status,  # type: ignore[arg-type]
            payment_method=checkout.payment_method,  # type: ignore[arg-type]
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
        )

    async def subscription_status(self, *, business_id: UUID) -> SubscriptionStatusResponse:
        subscription = await self.db.scalar(
            select(CommercialSubscription)
            .where(
                CommercialSubscription.business_id == business_id,
                CommercialSubscription.provider_environment == self.provider_environment,
            )
            .order_by(CommercialSubscription.created_at.desc())
            .limit(1)
        )
        if subscription is None:
            return SubscriptionStatusResponse(status="none")
        return SubscriptionStatusResponse(
            status=subscription.status,  # type: ignore[arg-type]
            payment_method=subscription.payment_method,  # type: ignore[arg-type]
            plan=subscription.plan_code,  # type: ignore[arg-type]
            cycle=subscription.billing_cycle,  # type: ignore[arg-type]
            access_until=subscription.access_until,
        )

    async def activate_paid_checkout(self, provider_checkout_id: str) -> None:
        checkout = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_checkout_id == provider_checkout_id,
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if checkout is None or checkout.payment_method != "credit_card":
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
                and _payment_value_cents(row.get("value")) == checkout.amount_cents
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

        subscription = await self._subscription_for_checkout(checkout.id)
        if subscription is None:
            subscription = CommercialSubscription(
                id=uuid4(),
                business_id=checkout.business_id,
                checkout_id=checkout.id,
                plan_code=checkout.plan_code,
                billing_cycle=checkout.billing_cycle,
                payment_method="credit_card",
                provider_environment=self.provider_environment,
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

    async def apply_pix_authorization_event(self, event_type: str, payload: dict) -> None:
        provider_id = payload.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            return
        checkout = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_authorization_id == provider_id,
                BillingCheckout.payment_method == "pix_automatic",
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if checkout is None:
            return

        if event_type == "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED":
            if not self._valid_pix_authorization(checkout, payload):
                raise AsaasGatewayError("Pix Automatic authorization reconciliation failed")
            checkout.status = "paid"
            checkout.paid_at = checkout.paid_at or datetime.now(UTC)
            subscription = await self._subscription_for_checkout(checkout.id)
            if subscription is None:
                subscription = CommercialSubscription(
                    id=uuid4(),
                    business_id=checkout.business_id,
                    checkout_id=checkout.id,
                    plan_code=checkout.plan_code,
                    billing_cycle=checkout.billing_cycle,
                    payment_method="pix_automatic",
                    provider_environment=self.provider_environment,
                    status="active",
                    provider_authorization_id=provider_id,
                    provider_customer_id=checkout.provider_customer_id,
                    access_until=_cycle_end(datetime.now(UTC), checkout.billing_cycle),
                )
                self.db.add(subscription)
            else:
                subscription.status = "active"
                subscription.provider_authorization_id = provider_id
                subscription.provider_customer_id = checkout.provider_customer_id
                subscription.access_until = max(
                    subscription.access_until,
                    _cycle_end(datetime.now(UTC), checkout.billing_cycle),
                )
            await self._record_operational_history(checkout.business_id)
        elif event_type == "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CREATED":
            checkout.status = "active"
        elif event_type in {
            "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED",
            "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_EXPIRED",
        }:
            subscription = await self._subscription_for_checkout(checkout.id)
            if subscription is not None:
                subscription.status = "canceled"
                subscription.canceled_at = datetime.now(UTC)
            if checkout.status != "paid":
                checkout.status = "expired" if event_type.endswith("EXPIRED") else "canceled"
        elif event_type == "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED":
            if checkout.status != "paid":
                checkout.status = "failed"
        await self.db.commit()

    def _valid_pix_authorization(self, checkout: BillingCheckout, payload: dict) -> bool:
        customer_id = payload.get("customerId")
        if (
            checkout.provider_customer_id
            and isinstance(customer_id, str)
            and customer_id != checkout.provider_customer_id
        ):
            return False
        amount = _payment_value_cents(payload.get("value"))
        return amount == checkout.amount_cents

    async def apply_subscription_event(self, event_type: str, payload: dict) -> None:
        provider_id = payload.get("id")
        if not isinstance(provider_id, str):
            return
        subscription = await self.db.scalar(
            select(CommercialSubscription).where(
                CommercialSubscription.provider_subscription_id == provider_id,
                CommercialSubscription.provider_environment == self.provider_environment,
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
        subscription = await self._subscription_for_payment(payload)
        if subscription is None:
            return

        if event_type in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            expected = get_offer(subscription.plan_code, subscription.billing_cycle).amount_cents
            if _payment_value_cents(payload.get("value")) != expected:
                return
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

    async def _subscription_for_payment(self, payload: dict) -> CommercialSubscription | None:
        provider_subscription_id = payload.get("subscription")
        if isinstance(provider_subscription_id, str) and provider_subscription_id:
            subscription = await self.db.scalar(
                select(CommercialSubscription).where(
                    CommercialSubscription.provider_subscription_id == provider_subscription_id,
                    CommercialSubscription.provider_environment == self.provider_environment,
                )
            )
            if subscription is not None:
                return subscription

        provider_authorization_id = payload.get("pixAutomaticAuthorizationId")
        if isinstance(provider_authorization_id, str) and provider_authorization_id:
            subscription = await self.db.scalar(
                select(CommercialSubscription).where(
                    CommercialSubscription.provider_authorization_id == provider_authorization_id,
                    CommercialSubscription.provider_environment == self.provider_environment,
                )
            )
            if subscription is not None:
                return subscription

        customer_id = payload.get("customer")
        if not isinstance(customer_id, str) or not customer_id:
            return None
        return await self.db.scalar(
            select(CommercialSubscription)
            .where(
                CommercialSubscription.provider_customer_id == customer_id,
                CommercialSubscription.payment_method == "pix_automatic",
                CommercialSubscription.provider_environment == self.provider_environment,
            )
            .order_by(CommercialSubscription.created_at.desc())
            .limit(1)
        )

    async def _subscription_for_checkout(
        self, checkout_id: UUID
    ) -> CommercialSubscription | None:
        return await self.db.scalar(
            select(CommercialSubscription).where(
                CommercialSubscription.checkout_id == checkout_id,
                CommercialSubscription.provider_environment == self.provider_environment,
            )
        )

    async def _record_operational_history(self, business_id: UUID) -> None:
        # Sandbox billing must never change the real operational-history flag.
        if self.provider_environment != "production":
            return
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
        return CheckoutCreateResponse(
            checkout_id=checkout.id,
            payment_method=checkout.payment_method,  # type: ignore[arg-type]
            checkout_url=checkout.checkout_url,
            pix_authorization_id=checkout.provider_authorization_id,
            pix_payload=checkout.pix_qr_payload,
            pix_expires_at=checkout.pix_qr_expires_at,
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
            amount_cents=checkout.amount_cents,
        )


def _payment_value_cents(value: object) -> int | None:
    try:
        decimal_value = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if decimal_value <= 0:
        return None
    return int(decimal_value * 100)


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

from __future__ import annotations

import calendar
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas import (
    AsaasCardRejectedError,
    AsaasGateway,
    AsaasGatewayError,
    AsaasGatewayTimeoutError,
)
from app.billing.catalog import (
    BillingCatalogConfigurationError,
    BillingCycle,
    cycle_months,
    get_offer,
    native_card_checkout_is_enabled,
    payment_method_is_enabled,
    plan_is_enabled,
)
from app.billing.schemas import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CheckoutProfileResponse,
    CheckoutStatusResponse,
    CreditCardCheckoutRequest,
    CreditCardCheckoutResponse,
    SubscriptionStatusResponse,
)
from app.models import BillingCheckout, Business, BusinessAccess, CommercialSubscription
from app.models.billing import billing_provider_environment


BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
logger = logging.getLogger(__name__)


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
        if not plan_is_enabled(payload.plan):
            raise HTTPException(409, "Plan is not available yet")
        if not payment_method_is_enabled(payload.payment_method):
            raise HTTPException(409, "Payment method is not available yet")
        try:
            offer = get_offer(payload.plan, payload.cycle)
        except BillingCatalogConfigurationError:
            raise HTTPException(503, "Billing configuration is unavailable") from None
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
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        self.db.add(checkout)
        await self.db.commit()

        if (
            payload.payment_method == "credit_card"
            and native_card_checkout_is_enabled()
        ):
            checkout.status = "active"
            await self.db.commit()
            return self._checkout_response(checkout)

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
            "minutesToExpire": 10,
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
                    "description": f"Assinatura {offer.plan_name} - {_cycle_label_pt(offer.cycle)}",
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
                "immediateQrCode": {
                    "originalValue": offer.amount_cents / 100,
                    "expirationSeconds": 3600,
                },
                "paymentCreationMode": "SUBSCRIPTION",
                "retryPolicy": "ALLOW_THREE_IN_SEVEN_DAYS",
            }
        )
        checkout.provider_customer_id = customer_id
        checkout.provider_authorization_id = authorization.authorization_id
        checkout.provider_subscription_id = authorization.subscription_id
        checkout.pix_qr_payload = authorization.payload
        checkout.pix_conciliation_identifier = authorization.conciliation_identifier
        checkout.pix_qr_expires_at = authorization.expires_at
        checkout.expires_at = authorization.expires_at or checkout.expires_at

    async def checkout_profile(
        self, *, business_id: UUID, payer_email: str
    ) -> CheckoutProfileResponse:
        business = await self.db.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Business not found")
        return CheckoutProfileResponse(
            email=payer_email,
            business_name=business.name,
            payer_name=business.responsible_name,
            postal_code=business.service_origin_postal_code,
            address_number=business.service_origin_number,
        )

    async def pay_credit_card(
        self,
        *,
        business_id: UUID,
        payer_email: str,
        checkout_id: UUID,
        payload: CreditCardCheckoutRequest,
        remote_ip: str,
    ) -> CreditCardCheckoutResponse:
        if not native_card_checkout_is_enabled():
            raise HTTPException(409, "Native card checkout is not available yet")
        if await self._admin_full_access_is_active(business_id):
            raise HTTPException(
                409,
                "Admin full access is active; a paid plan is not required",
            )

        checkout = await self.db.get(BillingCheckout, checkout_id)
        if (
            checkout is None
            or checkout.business_id != business_id
            or checkout.provider_environment != self.provider_environment
            or checkout.payment_method != "credit_card"
        ):
            raise HTTPException(404, "Checkout not found")
        if checkout.checkout_url is not None:
            raise HTTPException(409, "Checkout is not a native card session")
        if checkout.status == "paid":
            return CreditCardCheckoutResponse(
                checkout_id=checkout.id,
                status="paid",
                expires_at=checkout.expires_at,
            )
        if checkout.expires_at is not None and checkout.expires_at <= datetime.now(UTC):
            if checkout.status != "creating":
                checkout.status = "expired"
                await self.db.commit()
            raise HTTPException(409, "Checkout expired")
        if checkout.status in {"canceled", "expired", "failed"}:
            raise HTTPException(409, "Checkout is no longer available")
        if checkout.provider_subscription_id:
            return CreditCardCheckoutResponse(
                checkout_id=checkout.id,
                status="active",
                expires_at=checkout.expires_at,
            )

        external_reference = f"alovia:{checkout.id}"
        if checkout.status == "creating":
            recovered = await self._recover_native_subscription(
                checkout=checkout,
                external_reference=external_reference,
            )
            if recovered:
                return CreditCardCheckoutResponse(
                    checkout_id=checkout.id,
                    status=checkout.status,  # type: ignore[arg-type]
                    expires_at=checkout.expires_at,
                )
            raise HTTPException(
                409,
                "Payment confirmation is still being reconciled",
            )

        customer_reference = f"alovia:{checkout.business_id}"
        try:
            customer_id = await self.gateway.find_customer(
                external_reference=customer_reference,
                cpf_cnpj=payload.payer_cpf_cnpj,
            )
            if customer_id is None:
                customer_id = await self.gateway.create_customer(
                    name=payload.payer_name,
                    cpf_cnpj=payload.payer_cpf_cnpj,
                    email=payer_email,
                    external_reference=customer_reference,
                )
        except AsaasGatewayError:
            raise HTTPException(
                502, "Payment service is temporarily unavailable"
            ) from None

        checkout.provider_customer_id = customer_id
        checkout.status = "creating"
        await self.db.commit()

        offer = get_offer(checkout.plan_code, checkout.billing_cycle)
        provider_payload = {
            "customer": customer_id,
            "billingType": "CREDIT_CARD",
            "value": offer.amount_cents / 100,
            "nextDueDate": datetime.now(BRAZIL_TZ).date().isoformat(),
            "cycle": offer.asaas_cycle,
            "description": f"ALOVIA {offer.plan_name}",
            "externalReference": external_reference,
            "creditCard": {
                "holderName": payload.card_holder_name,
                "number": payload.card_number.get_secret_value(),
                "expiryMonth": payload.card_expiry_month,
                "expiryYear": payload.card_expiry_year,
                "ccv": payload.card_ccv.get_secret_value(),
            },
            "creditCardHolderInfo": {
                "name": payload.payer_name,
                "email": payer_email,
                "cpfCnpj": payload.payer_cpf_cnpj,
                "postalCode": payload.payer_postal_code,
                "addressNumber": payload.payer_address_number,
                "addressComplement": payload.payer_address_complement,
                "phone": None,
                "mobilePhone": payload.payer_phone,
            },
            "remoteIp": remote_ip,
        }

        try:
            created = await self.gateway.create_credit_card_subscription(
                provider_payload
            )
        except AsaasCardRejectedError:
            checkout.status = "active"
            await self.db.commit()
            raise HTTPException(
                422,
                "Card was not authorized. Check the data or use another card.",
            ) from None
        except (AsaasGatewayTimeoutError, AsaasGatewayError):
            recovered = await self._recover_native_subscription(
                checkout=checkout,
                external_reference=external_reference,
            )
            if recovered:
                return CreditCardCheckoutResponse(
                    checkout_id=checkout.id,
                    status=checkout.status,  # type: ignore[arg-type]
                    expires_at=checkout.expires_at,
                )
            # Keep "creating": provider outcome may be uncertain. A repeated
            # attempt must reconcile by externalReference before any new charge.
            await self.db.commit()
            raise HTTPException(
                502,
                "Payment confirmation is still being reconciled",
            ) from None

        checkout.provider_subscription_id = created.subscription_id
        checkout.status = "active"
        await self.db.commit()
        await self._reconcile_native_payment(checkout)
        return CreditCardCheckoutResponse(
            checkout_id=checkout.id,
            status=checkout.status,  # type: ignore[arg-type]
            expires_at=checkout.expires_at,
        )

    async def _recover_native_subscription(
        self,
        *,
        checkout: BillingCheckout,
        external_reference: str,
    ) -> bool:
        try:
            subscription_id = await self.gateway.find_subscription(
                external_reference=external_reference
            )
        except AsaasGatewayError:
            return False
        if not subscription_id:
            return False
        checkout.provider_subscription_id = subscription_id
        checkout.status = "active"
        await self.db.commit()
        await self._reconcile_native_payment(checkout)
        return True

    async def _reconcile_native_payment(self, checkout: BillingCheckout) -> None:
        if not checkout.provider_subscription_id or checkout.status == "paid":
            return
        try:
            payments = await self.gateway.payments_for_subscription(
                checkout.provider_subscription_id
            )
        except AsaasGatewayError:
            return
        for payment in payments:
            if _payment_value_cents(payment.get("value")) != checkout.amount_cents:
                continue
            if payment.get("status") not in {"CONFIRMED", "RECEIVED"}:
                continue
            checkout.status = "paid"
            checkout.paid_at = checkout.paid_at or datetime.now(UTC)
            provider_customer_id = payment.get("customer")
            if (
                isinstance(provider_customer_id, str)
                and 0 < len(provider_customer_id) <= 80
            ):
                checkout.provider_customer_id = provider_customer_id
            await self._activate_credit_card_subscription_if_ready(checkout)
            await self.db.commit()
            return

    async def _admin_full_access_is_active(self, business_id: UUID) -> bool:
        access = await self.db.get(BusinessAccess, business_id)
        if access is None:
            return False
        # Explicit admin grants created before admin_full_access was introduced
        # remain represented by access_mode="paid". Treat both representations
        # as an active administrative grant for checkout purposes. Revocation
        # writes access_mode="free" and admin_full_access=False, so normal
        # commercial checkout becomes available again immediately.
        return bool(access.admin_full_access or access.access_mode == "paid")

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
        if (
            checkout.status == "active"
            and checkout.expires_at is not None
            and checkout.expires_at <= datetime.now(UTC)
            and not checkout.provider_subscription_id
        ):
            checkout.status = "expired"
            await self.db.commit()
        return CheckoutStatusResponse(
            checkout_id=checkout.id,
            status=checkout.status,  # type: ignore[arg-type]
            payment_method=checkout.payment_method,  # type: ignore[arg-type]
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
            expires_at=checkout.expires_at,
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

        if checkout.status != "paid":
            checkout.status = "paid"
            checkout.paid_at = checkout.paid_at or datetime.now(UTC)

        # CHECKOUT_PAID is the provider's authoritative checkout result.
        # Never block its webhook acknowledgement on a secondary provider query:
        # payment/subscription references are correlated from payment events.
        await self._activate_credit_card_subscription_if_ready(checkout)
        await self.db.commit()

    async def _activate_credit_card_subscription_if_ready(
        self, checkout: BillingCheckout
    ) -> CommercialSubscription | None:
        if (
            checkout.payment_method != "credit_card"
            or checkout.status != "paid"
            or not checkout.provider_subscription_id
        ):
            return None

        subscription = await self._subscription_for_checkout(checkout.id)
        access_until = _cycle_end(datetime.now(UTC), checkout.billing_cycle)
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
                provider_subscription_id=checkout.provider_subscription_id,
                provider_customer_id=checkout.provider_customer_id,
                access_until=access_until,
            )
            self.db.add(subscription)
        else:
            subscription.status = "active"
            subscription.provider_subscription_id = checkout.provider_subscription_id
            subscription.provider_customer_id = checkout.provider_customer_id
            subscription.access_until = max(subscription.access_until, access_until)

        await self._record_operational_history(checkout.business_id)
        return subscription

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

        provider_subscription_id = payload.get("subscriptionId")
        if (
            isinstance(provider_subscription_id, str)
            and provider_subscription_id
            and len(provider_subscription_id) <= 80
        ):
            checkout.provider_subscription_id = provider_subscription_id

        if event_type == "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED":
            if not self._valid_pix_authorization(checkout, payload):
                checkout.status = "failed"
                logger.warning(
                    "asaas_pix_authorization_reconciliation_rejected",
                    extra={"provider_authorization_id": provider_id},
                )
                await self.db.commit()
                return
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
                    provider_subscription_id=checkout.provider_subscription_id,
                    provider_customer_id=checkout.provider_customer_id,
                    access_until=_cycle_end(datetime.now(UTC), checkout.billing_cycle),
                )
                self.db.add(subscription)
            else:
                subscription.status = "active"
                subscription.provider_authorization_id = provider_id
                if checkout.provider_subscription_id:
                    subscription.provider_subscription_id = checkout.provider_subscription_id
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
            if (
                provider_status == "ACTIVE"
                and subscription.status not in {"suspended", "past_due"}
            ):
                subscription.status = "active"
        await self.db.commit()

    async def apply_payment_event(self, event_type: str, payload: dict) -> None:
        checkout = await self._capture_credit_card_checkout_refs(payload)
        if checkout is not None:
            if (
                checkout.provider_checkout_id is None
                and event_type in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}
            ):
                checkout.status = "paid"
                checkout.paid_at = checkout.paid_at or datetime.now(UTC)
            await self._activate_credit_card_subscription_if_ready(checkout)

        subscription = await self._subscription_for_payment(payload)
        if subscription is None:
            # PAYMENT_CREATED can legitimately arrive before CHECKOUT_PAID.
            # Persist any provider references captured above and wait for the
            # complementary webhook instead of polling Asaas.
            await self.db.commit()
            return

        if event_type in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            expected = get_offer(subscription.plan_code, subscription.billing_cycle).amount_cents
            if _payment_value_cents(payload.get("value")) != expected:
                await self.db.commit()
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

    async def _capture_credit_card_checkout_refs(
        self, payload: dict
    ) -> BillingCheckout | None:
        checkout_session = payload.get("checkoutSession")
        provider_subscription_id = payload.get("subscription")
        if (
            not isinstance(provider_subscription_id, str)
            or not provider_subscription_id
            or len(provider_subscription_id) > 80
        ):
            return None

        checkout: BillingCheckout | None = None
        if (
            isinstance(checkout_session, str)
            and checkout_session
            and len(checkout_session) <= 80
        ):
            checkout = await self.db.scalar(
                select(BillingCheckout).where(
                    BillingCheckout.provider_checkout_id == checkout_session,
                    BillingCheckout.payment_method == "credit_card",
                    BillingCheckout.provider_environment == self.provider_environment,
                )
            )
        if checkout is None:
            checkout = await self.db.scalar(
                select(BillingCheckout).where(
                    BillingCheckout.provider_subscription_id
                    == provider_subscription_id,
                    BillingCheckout.payment_method == "credit_card",
                    BillingCheckout.provider_environment
                    == self.provider_environment,
                )
            )
        if checkout is None:
            return None
        if _payment_value_cents(payload.get("value")) != checkout.amount_cents:
            logger.warning(
                "asaas_payment_checkout_amount_mismatch",
                extra={
                    "provider_checkout_id": checkout_session,
                    "provider_subscription_id": provider_subscription_id,
                },
            )
            return None

        checkout.provider_subscription_id = provider_subscription_id
        provider_customer_id = payload.get("customer")
        if (
            isinstance(provider_customer_id, str)
            and 0 < len(provider_customer_id) <= 80
        ):
            checkout.provider_customer_id = provider_customer_id
        return checkout

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
        if checkout.payment_method == "pix_automatic":
            checkout_mode = "pix"
        elif checkout.checkout_url:
            checkout_mode = "hosted"
        else:
            checkout_mode = "native"
        return CheckoutCreateResponse(
            checkout_id=checkout.id,
            payment_method=checkout.payment_method,  # type: ignore[arg-type]
            checkout_mode=checkout_mode,  # type: ignore[arg-type]
            checkout_url=checkout.checkout_url,
            pix_authorization_id=checkout.provider_authorization_id,
            pix_payload=checkout.pix_qr_payload,
            pix_expires_at=checkout.pix_qr_expires_at,
            expires_at=checkout.expires_at,
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

def _cycle_label_pt(cycle: str) -> str:
    return {
        "monthly": "mensal",
        "quarterly": "trimestral",
        "annual": "anual",
    }.get(cycle, cycle)
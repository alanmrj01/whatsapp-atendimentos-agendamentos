from __future__ import annotations

import calendar
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text
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
from app.models import BillingCheckout, Business, BusinessAccess, CommercialSubscription, WebPushEvent
from app.models.billing import billing_provider_environment


BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
logger = logging.getLogger(__name__)


class BillingService:
    def __init__(self, db: AsyncSession, gateway: AsaasGateway) -> None:
        self.db = db
        self.gateway = gateway
        self.provider_environment = billing_provider_environment()

    async def _lock_business_checkout(self, business_id: UUID) -> None:
        execute = getattr(self.db, "execute", None)
        if execute is None:
            return
        await execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {
                "lock_key": (
                    f"alovia:billing:{self.provider_environment}:{business_id}"
                )
            },
        )

    async def _blocking_checkout_for_business(
        self, business_id: UUID
    ) -> BillingCheckout | None:
        return await self.db.scalar(
            select(BillingCheckout)
            .where(
                BillingCheckout.business_id == business_id,
                BillingCheckout.provider_environment == self.provider_environment,
                or_(
                    BillingCheckout.status.in_(("creating", "active")),
                    and_(
                        BillingCheckout.payment_method == "pix_automatic",
                        BillingCheckout.paid_at.is_not(None),
                        BillingCheckout.status != "paid",
                    ),
                    and_(
                        BillingCheckout.payment_method == "pix_automatic",
                        # Legacy/local expiry is not proof that the provider
                        # authorization is terminal. Require explicit cancel.
                        BillingCheckout.status == "expired",
                        BillingCheckout.provider_authorization_id.is_not(None),
                    ),
                    and_(
                        BillingCheckout.payment_method == "pix",
                        BillingCheckout.status == "expired",
                        BillingCheckout.provider_subscription_id.is_not(None),
                    ),
                ),
            )
            .order_by(BillingCheckout.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def _reusable_pix_checkout(checkout: BillingCheckout) -> bool:
        return bool(
            checkout.status in {"creating", "active"}
            and checkout.paid_at is None
            and checkout.provider_authorization_id
            and checkout.pix_qr_payload
            and checkout.expires_at is not None
            and checkout.expires_at > datetime.now(UTC)
        )

    @staticmethod
    def _reusable_common_pix_checkout(checkout: BillingCheckout) -> bool:
        return bool(
            checkout.status in {"creating", "active"}
            and checkout.paid_at is None
            and checkout.provider_subscription_id
            and checkout.pix_qr_payload
            and checkout.expires_at is not None
            and checkout.expires_at > datetime.now(UTC)
        )

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
        await self._lock_business_checkout(business_id)
        existing = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.business_id == business_id,
                BillingCheckout.idempotency_key == idempotency_key,
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if existing is not None:
            if (
                existing.plan_code != offer.plan
                or existing.billing_cycle != offer.cycle
                or existing.payment_method != payload.payment_method
            ):
                await self.db.rollback()
                raise HTTPException(409, "Idempotency key already used")
            if (
                existing.payment_method in {"pix", "pix_automatic"}
                and existing.paid_at is not None
            ):
                await self.db.commit()
                return self._checkout_response(existing)
            if existing.status in {"active", "paid"}:
                await self.db.commit()
                return self._checkout_response(existing)
            await self.db.rollback()
            raise HTTPException(409, "Checkout is still being prepared")

        if await self._active_subscription_for_business(business_id) is not None:
            await self.db.rollback()
            raise HTTPException(409, "Commercial subscription is already active")

        blocking_checkout = await self._blocking_checkout_for_business(business_id)
        if blocking_checkout is not None:
            if blocking_checkout.paid_at is not None:
                if (
                    payload.payment_method in {"pix", "pix_automatic"}
                    and blocking_checkout.payment_method == payload.payment_method
                    and blocking_checkout.plan_code == offer.plan
                    and blocking_checkout.billing_cycle == offer.cycle
                ):
                    await self.db.commit()
                    return self._checkout_response(blocking_checkout)
                await self.db.rollback()
                raise HTTPException(
                    409,
                    "Initial Pix payment is awaiting authorization reconciliation",
                )
            elif blocking_checkout.payment_method == "credit_card":
                same_card_offer = (
                    payload.payment_method == "credit_card"
                    and blocking_checkout.plan_code == offer.plan
                    and blocking_checkout.billing_cycle == offer.cycle
                )
                if not same_card_offer or blocking_checkout.status != "active":
                    await self.db.rollback()
                    raise HTTPException(409, "A card checkout is already active")
                if blocking_checkout.provider_subscription_id:
                    await self.db.commit()
                    return self._checkout_response(blocking_checkout)
                if (
                    blocking_checkout.expires_at is None
                    or blocking_checkout.expires_at > datetime.now(UTC)
                ):
                    await self.db.commit()
                    return self._checkout_response(blocking_checkout)
                # A card session that never reached a provider subscription and
                # has passed its local/provider TTL must not block this business
                # forever. Expire it inside the same transaction and continue.
                blocking_checkout.status = "expired"
            elif blocking_checkout.payment_method == "pix":
                same_pix_offer = (
                    payload.payment_method == "pix"
                    and blocking_checkout.plan_code == offer.plan
                    and blocking_checkout.billing_cycle == offer.cycle
                )
                if same_pix_offer and self._reusable_common_pix_checkout(blocking_checkout):
                    await self.db.commit()
                    return self._checkout_response(blocking_checkout)
                if (
                    same_pix_offer
                    and blocking_checkout.status in {"creating", "active"}
                    and blocking_checkout.provider_subscription_id
                ):
                    recovered = await self._recover_common_pix_checkout(blocking_checkout)
                    if recovered:
                        await self.db.commit()
                        return self._checkout_response(blocking_checkout)
                    await self.db.rollback()
                    raise HTTPException(409, "Pix payment is being reconciled")
                await self.db.rollback()
                raise HTTPException(
                    409,
                    "Cancel the active Pix checkout before starting another payment",
                )
            elif blocking_checkout.payment_method == "pix_automatic":
                if (
                    payload.payment_method == "pix_automatic"
                    and self._reusable_pix_checkout(blocking_checkout)
                ):
                    if (
                        blocking_checkout.plan_code != offer.plan
                        or blocking_checkout.billing_cycle != offer.cycle
                    ):
                        await self.db.rollback()
                        raise HTTPException(
                            409, "Another Pix Automatic checkout is already active"
                        )
                    await self.db.commit()
                    return self._checkout_response(blocking_checkout)
                await self.db.rollback()
                raise HTTPException(
                    409,
                    "Cancel the active Pix Automatic checkout before starting another payment",
                )
            else:
                await self.db.rollback()
                raise HTTPException(409, "Another checkout is already active")

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
            elif payload.payment_method == "pix":
                await self._prepare_common_pix(
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
        except AsaasGatewayTimeoutError:
            if payload.payment_method == "pix_automatic":
                # The provider may have created the authorization even when its
                # response did not reach us. Keep this checkout blocking until
                # a webhook reconciles contractId, rather than allowing a
                # second financial attempt.
                checkout.status = "creating"
                await self.db.commit()
                raise HTTPException(
                    502, "Pix Automatic authorization is being reconciled"
                ) from None
            if payload.payment_method == "pix" and checkout.provider_subscription_id:
                checkout.status = "creating"
                await self.db.commit()
                raise HTTPException(
                    502, "Pix payment is being reconciled"
                ) from None
            checkout.status = "failed"
            await self.db.commit()
            raise HTTPException(502, "Payment checkout is temporarily unavailable") from None
        except AsaasGatewayError:
            if payload.payment_method == "pix" and checkout.provider_subscription_id:
                checkout.status = "creating"
                await self.db.commit()
                raise HTTPException(
                    502, "Pix payment is being reconciled"
                ) from None
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

    async def _prepare_common_pix(
        self,
        *,
        checkout: BillingCheckout,
        offer,
        payer_name: str,
        payer_cpf_cnpj: str,
        payer_email: str,
    ) -> None:
        customer_reference = f"alovia:{checkout.business_id}"
        customer_id = await self.gateway.find_customer(
            external_reference=customer_reference,
            cpf_cnpj=payer_cpf_cnpj,
        )
        if customer_id is None:
            customer_id = await self.gateway.create_customer(
                name=payer_name,
                cpf_cnpj=payer_cpf_cnpj,
                email=payer_email,
                external_reference=customer_reference,
            )
        checkout.provider_customer_id = customer_id

        subscription_reference = f"alovia:{checkout.id}"
        provider_payload = {
            "customer": customer_id,
            "billingType": "PIX",
            "value": offer.amount_cents / 100,
            "nextDueDate": datetime.now(BRAZIL_TZ).date().isoformat(),
            "cycle": offer.asaas_cycle,
            "description": f"ALOVIA {offer.plan_name}",
            "externalReference": subscription_reference,
        }
        try:
            created = await self.gateway.create_pix_subscription(provider_payload)
            checkout.provider_subscription_id = created.subscription_id
        except (AsaasGatewayTimeoutError, AsaasGatewayError):
            recovered_id = await self.gateway.find_subscription(
                external_reference=subscription_reference
            )
            if not recovered_id:
                raise
            checkout.provider_subscription_id = recovered_id

        if not await self._recover_common_pix_checkout(checkout):
            raise AsaasGatewayError("Asaas Pix payment is not ready")

    async def _recover_common_pix_checkout(
        self, checkout: BillingCheckout
    ) -> bool:
        if not checkout.provider_subscription_id:
            return False
        try:
            payments = await self.gateway.payments_for_subscription(
                checkout.provider_subscription_id
            )
        except AsaasGatewayError:
            return False

        for payment in payments:
            if _payment_value_cents(payment.get("value")) != checkout.amount_cents:
                continue
            payment_id = payment.get("id")
            if not isinstance(payment_id, str) or not payment_id:
                continue
            customer_id = payment.get("customer")
            if isinstance(customer_id, str) and 0 < len(customer_id) <= 80:
                checkout.provider_customer_id = customer_id

            if payment.get("status") in {"CONFIRMED", "RECEIVED"}:
                checkout.status = "paid"
                checkout.paid_at = checkout.paid_at or datetime.now(UTC)
                await self._activate_common_pix_subscription_if_ready(checkout)
                return True

            try:
                qr = await self.gateway.pix_qr_code(payment_id)
            except AsaasGatewayError:
                continue
            checkout.pix_qr_payload = qr.payload
            checkout.pix_qr_expires_at = qr.expires_at
            checkout.expires_at = qr.expires_at or checkout.expires_at
            checkout.status = "active"
            return True
        return False

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
            pix_available=payment_method_is_enabled("pix"),
            pix_automatic_available=payment_method_is_enabled("pix_automatic"),
            native_card_checkout_available=native_card_checkout_is_enabled(),
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

        # Serialize the entire native-card financial attempt. In particular, do
        # not commit after switching the checkout to "creating": that would
        # release the transaction-scoped advisory lock before the provider call.
        await self._lock_business_checkout(business_id)
        checkout = await self.db.get(BillingCheckout, checkout_id)
        if (
            checkout is None
            or checkout.business_id != business_id
            or checkout.provider_environment != self.provider_environment
            or checkout.payment_method != "credit_card"
        ):
            await self.db.rollback()
            raise HTTPException(404, "Checkout not found")
        if checkout.checkout_url is not None:
            await self.db.rollback()
            raise HTTPException(409, "Checkout is not a native card session")
        if checkout.status == "paid":
            return CreditCardCheckoutResponse(
                checkout_id=checkout.id,
                status="paid",
                expires_at=checkout.expires_at,
            )

        active_subscription = await self._active_subscription_for_business(business_id)
        if (
            active_subscription is not None
            and active_subscription.checkout_id != checkout.id
        ):
            await self.db.rollback()
            raise HTTPException(409, "Commercial subscription is already active")

        if checkout.expires_at is not None and checkout.expires_at <= datetime.now(UTC):
            if checkout.status != "creating":
                checkout.status = "expired"
                await self.db.commit()
            raise HTTPException(409, "Checkout expired")
        if checkout.status in {"canceled", "expired", "failed"}:
            await self.db.rollback()
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
            await self.db.rollback()
            raise HTTPException(
                502, "Payment service is temporarily unavailable"
            ) from None

        checkout.provider_customer_id = customer_id
        checkout.status = "creating"

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
        # Pix QR expiry is visual only. Provider reconciliation or explicit
        # cancellation is required before another financial attempt is safe.
        if (
            checkout.payment_method == "credit_card"
            and checkout.status == "active"
            and checkout.expires_at is not None
            and checkout.expires_at <= datetime.now(UTC)
            and not checkout.provider_subscription_id
        ):
            checkout.status = "expired"
            await self.db.commit()
        return self._checkout_status_response(checkout)

    async def cancel_checkout(
        self, *, business_id: UUID, checkout_id: UUID
    ) -> CheckoutStatusResponse:
        await self._lock_business_checkout(business_id)
        checkout = await self.db.get(BillingCheckout, checkout_id)
        if (
            checkout is None
            or checkout.business_id != business_id
            or checkout.provider_environment != self.provider_environment
        ):
            await self.db.rollback()
            raise HTTPException(404, "Checkout not found")
        if checkout.payment_method not in {"pix", "pix_automatic"}:
            await self.db.rollback()
            raise HTTPException(409, "Only Pix checkout can be canceled")
        if checkout.status == "paid" or checkout.paid_at is not None:
            await self.db.rollback()
            raise HTTPException(
                409, "Initial Pix payment cannot be canceled automatically"
            )
        if checkout.status in {"canceled", "failed"}:
            await self.db.commit()
            return self._checkout_status_response(checkout)
        try:
            if checkout.payment_method == "pix_automatic" and checkout.provider_authorization_id:
                await self.gateway.cancel_pix_authorization(
                    checkout.provider_authorization_id
                )
            elif checkout.payment_method == "pix" and checkout.provider_subscription_id:
                await self.gateway.cancel_subscription(
                    checkout.provider_subscription_id
                )
        except AsaasGatewayError:
            await self.db.rollback()
            raise HTTPException(
                502, "Pix cancellation is temporarily unavailable"
            ) from None
        checkout.status = "canceled"
        await self.db.commit()
        return self._checkout_status_response(checkout)

    @staticmethod
    def _checkout_status_response(
        checkout: BillingCheckout,
    ) -> CheckoutStatusResponse:
        return CheckoutStatusResponse(
            checkout_id=checkout.id,
            status=checkout.status,  # type: ignore[arg-type]
            payment_method=checkout.payment_method,  # type: ignore[arg-type]
            plan=checkout.plan_code,  # type: ignore[arg-type]
            cycle=checkout.billing_cycle,  # type: ignore[arg-type]
            expires_at=checkout.expires_at,
            initial_payment_received=checkout.paid_at is not None,
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

        await self._lock_business_checkout(checkout.business_id)
        subscription = await self._subscription_for_checkout(checkout.id)
        access_until = _cycle_end(datetime.now(UTC), checkout.billing_cycle)
        if subscription is None:
            active_subscription = await self._active_subscription_for_business(
                checkout.business_id
            )
            if active_subscription is not None:
                logger.warning(
                    "billing_active_subscription_conflict",
                    extra={
                        "payment_method": checkout.payment_method,
                        "provider_environment": self.provider_environment,
                    },
                )
                return None
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

    async def _activate_common_pix_subscription_if_ready(
        self, checkout: BillingCheckout
    ) -> CommercialSubscription | None:
        if (
            checkout.payment_method != "pix"
            or checkout.status != "paid"
            or not checkout.provider_subscription_id
        ):
            return None

        await self._lock_business_checkout(checkout.business_id)
        subscription = await self._subscription_for_checkout(checkout.id)
        access_until = _cycle_end(datetime.now(UTC), checkout.billing_cycle)
        if subscription is None:
            active_subscription = await self._active_subscription_for_business(
                checkout.business_id
            )
            if active_subscription is not None:
                logger.warning(
                    "billing_active_subscription_conflict",
                    extra={
                        "payment_method": checkout.payment_method,
                        "provider_environment": self.provider_environment,
                    },
                )
                return None
            subscription = CommercialSubscription(
                id=uuid4(),
                business_id=checkout.business_id,
                checkout_id=checkout.id,
                plan_code=checkout.plan_code,
                billing_cycle=checkout.billing_cycle,
                payment_method="pix",
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
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 100
        ):
            return

        checkout = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_authorization_id == provider_id,
                BillingCheckout.payment_method == "pix_automatic",
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )

        # A create-authorization timeout has an uncertain outcome. Asaas echoes
        # our deterministic checkout UUID in authorization.contractId, allowing
        # the webhook to bind the provider authorization back to the local
        # checkout without opening a second payment attempt.
        if checkout is None:
            contract_id = payload.get("contractId")
            if (
                not isinstance(contract_id, str)
                or len(contract_id) != 32
                or any(ch not in "0123456789abcdefABCDEF" for ch in contract_id)
            ):
                return
            try:
                checkout_id = UUID(hex=contract_id)
            except ValueError:
                return
            checkout = await self.db.get(BillingCheckout, checkout_id)
            if (
                checkout is None
                or checkout.payment_method != "pix_automatic"
                or checkout.provider_environment != self.provider_environment
            ):
                return

        await self._lock_business_checkout(checkout.business_id)
        checkout = await self.db.get(BillingCheckout, checkout.id)
        if (
            checkout is None
            or checkout.payment_method != "pix_automatic"
            or checkout.provider_environment != self.provider_environment
        ):
            return
        if (
            checkout.provider_authorization_id is not None
            and checkout.provider_authorization_id != provider_id
        ):
            logger.warning(
                "asaas_pix_authorization_id_conflict",
                extra={"provider_environment": self.provider_environment},
            )
            return
        checkout.provider_authorization_id = provider_id

        provider_subscription_id = payload.get("subscriptionId")
        if (
            isinstance(provider_subscription_id, str)
            and provider_subscription_id
            and len(provider_subscription_id) <= 80
        ):
            if (
                checkout.provider_subscription_id is not None
                and checkout.provider_subscription_id != provider_subscription_id
            ):
                logger.warning(
                    "asaas_pix_subscription_id_conflict",
                    extra={"provider_environment": self.provider_environment},
                )
                return
            checkout.provider_subscription_id = provider_subscription_id

        customer_id = payload.get("customerId")
        if isinstance(customer_id, str) and 0 < len(customer_id) <= 80:
            if (
                checkout.provider_customer_id is not None
                and checkout.provider_customer_id != customer_id
            ):
                logger.warning(
                    "asaas_pix_customer_id_conflict",
                    extra={"provider_environment": self.provider_environment},
                )
                return
            checkout.provider_customer_id = customer_id

        immediate = payload.get("immediateQrCode")
        if isinstance(immediate, dict):
            expires_at = _parse_provider_datetime(immediate.get("expirationDate"))
            if expires_at is not None:
                checkout.pix_qr_expires_at = expires_at
                checkout.expires_at = expires_at
            conciliation = immediate.get("conciliationIdentifier")
            if isinstance(conciliation, str) and 0 < len(conciliation) <= 100:
                checkout.pix_conciliation_identifier = conciliation

        qr_payload = payload.get("payload")
        if not isinstance(qr_payload, str) and isinstance(immediate, dict):
            qr_payload = immediate.get("payload")
        if (
            isinstance(qr_payload, str)
            and qr_payload.strip()
            and len(qr_payload) <= 8_000
        ):
            checkout.pix_qr_payload = qr_payload

        if event_type == "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED":
            if not self._valid_pix_authorization(checkout, payload):
                checkout.status = "failed"
                logger.warning(
                    "asaas_pix_authorization_reconciliation_rejected",
                    extra={"provider_environment": self.provider_environment},
                )
                await self.db.commit()
                return
            checkout.status = "paid"
            checkout.paid_at = checkout.paid_at or datetime.now(UTC)
            subscription = await self._subscription_for_checkout(checkout.id)
            if subscription is None:
                active_subscription = await self._active_subscription_for_business(
                    checkout.business_id
                )
                if active_subscription is not None:
                    logger.warning(
                        "billing_active_subscription_conflict",
                        extra={
                            "payment_method": checkout.payment_method,
                            "provider_environment": self.provider_environment,
                        },
                    )
                else:
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
                        access_until=_cycle_end(
                            datetime.now(UTC), checkout.billing_cycle
                        ),
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
            if subscription is not None:
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

    async def apply_payment_event(self, event_type: str, payload: dict) -> str | None:
        common_pix_checkout = await self._capture_common_pix_payment(
            event_type, payload
        )
        if common_pix_checkout is not None:
            await self._activate_common_pix_subscription_if_ready(
                common_pix_checkout
            )
        await self._capture_pix_initial_payment(event_type, payload)
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
            return None

        action_event_key: str | None = None
        if event_type in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            expected = get_offer(subscription.plan_code, subscription.billing_cycle).amount_cents
            if _payment_value_cents(payload.get("value")) != expected:
                await self.db.commit()
                return None
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
            was_past_due = subscription.status == "past_due"
            subscription.status = "past_due"
            provider_payment_id = payload.get("id")
            if (
                not was_past_due
                and isinstance(provider_payment_id, str)
                and 0 < len(provider_payment_id) <= 100
            ):
                action_event_key = (
                    f"billing-overdue:{self.provider_environment}:"
                    f"{provider_payment_id}"
                )
                self.db.add(
                    WebPushEvent(
                        business_id=subscription.business_id,
                        event_key=action_event_key,
                        event_type="billing_action",
                        target_path="/app/mais/plano",
                    )
                )
        elif event_type in {
            "PAYMENT_REFUNDED",
            "PAYMENT_CHARGEBACK_REQUESTED",
            "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
        }:
            subscription.status = "suspended"
            subscription.access_until = min(subscription.access_until, datetime.now(UTC))
        await self.db.commit()
        return action_event_key

    async def _capture_common_pix_payment(
        self, event_type: str, payload: dict
    ) -> BillingCheckout | None:
        if event_type not in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            return None
        provider_subscription_id = payload.get("subscription")
        if (
            not isinstance(provider_subscription_id, str)
            or not provider_subscription_id
            or len(provider_subscription_id) > 80
        ):
            return None
        checkout = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_subscription_id == provider_subscription_id,
                BillingCheckout.payment_method == "pix",
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if checkout is None:
            return None
        await self._lock_business_checkout(checkout.business_id)
        checkout = await self.db.scalar(
            select(BillingCheckout)
            .where(
                BillingCheckout.id == checkout.id,
                BillingCheckout.payment_method == "pix",
                BillingCheckout.provider_environment == self.provider_environment,
            )
            .execution_options(populate_existing=True)
        )
        if checkout is None:
            return None
        if _payment_value_cents(payload.get("value")) != checkout.amount_cents:
            logger.warning(
                "asaas_pix_payment_amount_mismatch",
                extra={"provider_environment": self.provider_environment},
            )
            return None
        customer_id = payload.get("customer")
        if isinstance(customer_id, str) and 0 < len(customer_id) <= 80:
            if (
                checkout.provider_customer_id is not None
                and checkout.provider_customer_id != customer_id
            ):
                return None
            checkout.provider_customer_id = customer_id
        checkout.status = "paid"
        checkout.paid_at = checkout.paid_at or datetime.now(UTC)
        return checkout

    async def _capture_pix_initial_payment(
        self, event_type: str, payload: dict
    ) -> BillingCheckout | None:
        if event_type not in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            return None
        checkout = await self._pix_checkout_for_payment(payload)
        if checkout is None:
            return None
        await self._lock_business_checkout(checkout.business_id)
        checkout = await self.db.scalar(
            select(BillingCheckout)
            .where(
                BillingCheckout.id == checkout.id,
                BillingCheckout.payment_method == "pix_automatic",
                BillingCheckout.provider_environment == self.provider_environment,
            )
            .execution_options(populate_existing=True)
        )
        if (
            checkout is None
            or checkout.payment_method != "pix_automatic"
            or checkout.provider_environment != self.provider_environment
        ):
            return None
        provider_authorization_id = payload.get("pixAutomaticAuthorizationId")
        if (
            provider_authorization_id is not None
            and provider_authorization_id != checkout.provider_authorization_id
        ):
            return None
        provider_subscription_id = payload.get("subscription")
        if (
            provider_authorization_id is None
            and provider_subscription_id is not None
            and provider_subscription_id != checkout.provider_subscription_id
        ):
            return None
        if _payment_value_cents(payload.get("value")) != checkout.amount_cents:
            logger.warning(
                "asaas_pix_initial_payment_amount_mismatch",
                extra={"provider_environment": self.provider_environment},
            )
            return None
        checkout.paid_at = checkout.paid_at or datetime.now(UTC)
        return checkout

    async def _pix_checkout_for_payment(
        self, payload: dict
    ) -> BillingCheckout | None:
        provider_authorization_id = payload.get("pixAutomaticAuthorizationId")
        if provider_authorization_id is not None:
            if (
                not isinstance(provider_authorization_id, str)
                or not provider_authorization_id
                or len(provider_authorization_id) > 100
            ):
                return None
            return await self.db.scalar(
                select(BillingCheckout).where(
                    BillingCheckout.provider_authorization_id
                    == provider_authorization_id,
                    BillingCheckout.payment_method == "pix_automatic",
                    BillingCheckout.provider_environment
                    == self.provider_environment,
                )
            )

        provider_subscription_id = payload.get("subscription")
        if provider_subscription_id is not None:
            if (
                not isinstance(provider_subscription_id, str)
                or not provider_subscription_id
                or len(provider_subscription_id) > 80
            ):
                return None
            return await self.db.scalar(
                select(BillingCheckout).where(
                    BillingCheckout.provider_subscription_id
                    == provider_subscription_id,
                    BillingCheckout.payment_method == "pix_automatic",
                    BillingCheckout.provider_environment
                    == self.provider_environment,
                )
            )

        provider_reference = payload.get("externalReference") or payload.get(
            "contractId"
        )
        if not isinstance(provider_reference, str):
            return None
        reference_value = provider_reference.removeprefix("alovia:")
        try:
            checkout_id = UUID(reference_value)
        except ValueError:
            return None
        checkout = await self.db.get(BillingCheckout, checkout_id)
        if (
            checkout is None
            or checkout.payment_method != "pix_automatic"
            or checkout.provider_environment != self.provider_environment
        ):
            return None
        return checkout

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
            # A Pix initial payment may arrive before authorization activation.
            # Never correlate it to an older subscription by customer fallback.
            return None

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

    async def _active_subscription_for_business(
        self, business_id: UUID
    ) -> CommercialSubscription | None:
        return await self.db.scalar(
            select(CommercialSubscription)
            .where(
                CommercialSubscription.business_id == business_id,
                CommercialSubscription.provider_environment
                == self.provider_environment,
                CommercialSubscription.status == "active",
            )
            .order_by(CommercialSubscription.created_at.desc())
            .limit(1)
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
        if checkout.payment_method in {"pix", "pix_automatic"}:
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
            initial_payment_received=checkout.paid_at is not None,
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


def _parse_provider_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRAZIL_TZ)
    return parsed.astimezone(UTC)


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

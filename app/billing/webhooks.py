from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas import AsaasGateway
from app.billing.service import BillingService
from app.models import BillingWebhookEvent

CHECKOUT_EVENTS = {
    "CHECKOUT_CREATED",
    "CHECKOUT_PAID",
    "CHECKOUT_CANCELED",
    "CHECKOUT_EXPIRED",
}
SUBSCRIPTION_EVENTS = {
    "SUBSCRIPTION_CREATED",
    "SUBSCRIPTION_UPDATED",
    "SUBSCRIPTION_INACTIVATED",
    "SUBSCRIPTION_DELETED",
}
PAYMENT_EVENTS = {
    "PAYMENT_CREATED",
    "PAYMENT_UPDATED",
    "PAYMENT_CONFIRMED",
    "PAYMENT_RECEIVED",
    "PAYMENT_OVERDUE",
    "PAYMENT_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_AWAITING_CHARGEBACK_REVERSAL",
}
PIX_AUTHORIZATION_EVENTS = {
    "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CREATED",
    "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED",
    "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED",
    "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_EXPIRED",
    "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED",
}
PIX_PAYMENT_INSTRUCTION_EVENTS = {
    "PIX_AUTOMATIC_RECURRING_PAYMENT_INSTRUCTION_CREATED",
    "PIX_AUTOMATIC_RECURRING_PAYMENT_INSTRUCTION_SCHEDULED",
    "PIX_AUTOMATIC_RECURRING_PAYMENT_INSTRUCTION_REFUSED",
    "PIX_AUTOMATIC_RECURRING_PAYMENT_INSTRUCTION_CANCELLED",
}
SUPPORTED_EVENTS = (
    CHECKOUT_EVENTS
    | SUBSCRIPTION_EVENTS
    | PAYMENT_EVENTS
    | PIX_AUTHORIZATION_EVENTS
    | PIX_PAYMENT_INSTRUCTION_EVENTS
)


class BillingWebhookService:
    def __init__(self, db: AsyncSession, gateway: AsaasGateway) -> None:
        self.db = db
        self.billing = BillingService(db, gateway)
        self.provider_environment = self.billing.provider_environment

    async def process(self, payload: dict) -> None:
        event_id = payload.get("id")
        event_type = payload.get("event")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(event_type, str)
            or not event_type
            or len(event_type) > 80
        ):
            return
        if event_type not in SUPPORTED_EVENTS:
            return

        storage_event_id = self._event_storage_id(
            self.provider_environment, event_id
        )
        if len(storage_event_id) > 160:
            return

        existing = await self.db.get(
            BillingWebhookEvent, storage_event_id
        )
        if existing is not None and existing.processed_at is not None:
            return

        resource_type, resource = self._resource(payload, event_type)
        resource_id = resource.get("id") if isinstance(resource, dict) else None
        if not isinstance(resource_id, str) or len(resource_id) > 100:
            resource_id = None
        provider_payment_id, provider_authorization_id = self._pix_instruction_refs(
            event_type, resource
        )

        if existing is None:
            existing = BillingWebhookEvent(
                event_id=storage_event_id,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_payment_id=provider_payment_id,
                provider_authorization_id=provider_authorization_id,
            )
            self.db.add(existing)
            await self.db.commit()
        else:
            existing.provider_payment_id = provider_payment_id
            existing.provider_authorization_id = provider_authorization_id

        if event_type in CHECKOUT_EVENTS:
            await self._checkout(event_type, resource)
        elif event_type in SUBSCRIPTION_EVENTS:
            await self.billing.apply_subscription_event(event_type, resource)
        elif event_type in PAYMENT_EVENTS:
            await self.billing.apply_payment_event(event_type, resource)
        elif event_type in PIX_AUTHORIZATION_EVENTS:
            await self.billing.apply_pix_authorization_event(event_type, resource)
        # Instruction events correlate authorization -> payment. Access changes only
        # after the authorization/payment lifecycle confirms the financial event.

        existing.processed_at = datetime.now(UTC)
        await self.db.commit()

    async def _checkout(self, event_type: str, checkout: dict) -> None:
        provider_id = checkout.get("id")
        if not isinstance(provider_id, str):
            return
        from sqlalchemy import select
        from app.models import BillingCheckout

        local = await self.db.scalar(
            select(BillingCheckout).where(
                BillingCheckout.provider_checkout_id == provider_id,
                BillingCheckout.provider_environment == self.provider_environment,
            )
        )
        if local is None:
            return
        if event_type == "CHECKOUT_PAID":
            await self.billing.activate_paid_checkout(provider_id)
            return
        if event_type == "CHECKOUT_CREATED":
            local.status = "active"
        elif event_type == "CHECKOUT_CANCELED":
            local.status = "canceled"
        elif event_type == "CHECKOUT_EXPIRED":
            local.status = "expired"
        await self.db.commit()

    @staticmethod
    def _event_storage_id(
        provider_environment: str, provider_event_id: str
    ) -> str:
        return f"{provider_environment}:{provider_event_id}"

    @staticmethod
    def _pix_instruction_refs(event_type: str, resource: dict) -> tuple[str | None, str | None]:
        if event_type not in PIX_PAYMENT_INSTRUCTION_EVENTS:
            return None, None
        payment_id = resource.get("paymentId") or resource.get("payment")
        authorization = resource.get("authorization")
        authorization_id = authorization.get("id") if isinstance(authorization, dict) else None
        if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 100:
            payment_id = None
        if (
            not isinstance(authorization_id, str)
            or not authorization_id
            or len(authorization_id) > 100
        ):
            authorization_id = None
        return payment_id, authorization_id

    @staticmethod
    def _resource(payload: dict, event_type: str) -> tuple[str, dict]:
        if event_type in CHECKOUT_EVENTS:
            resource = payload.get("checkout")
            return "checkout", resource if isinstance(resource, dict) else {}
        if event_type in SUBSCRIPTION_EVENTS:
            resource = payload.get("subscription")
            return "subscription", resource if isinstance(resource, dict) else {}
        if event_type in PIX_AUTHORIZATION_EVENTS:
            resource = payload.get("authorization")
            return "pix_authorization", resource if isinstance(resource, dict) else {}
        if event_type in PIX_PAYMENT_INSTRUCTION_EVENTS:
            resource = payload.get("paymentInstruction")
            return "pix_payment_instruction", resource if isinstance(resource, dict) else {}
        resource = payload.get("payment")
        return "payment", resource if isinstance(resource, dict) else {}

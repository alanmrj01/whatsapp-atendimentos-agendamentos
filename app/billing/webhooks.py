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
SUPPORTED_EVENTS = CHECKOUT_EVENTS | SUBSCRIPTION_EVENTS | PAYMENT_EVENTS


class BillingWebhookService:
    def __init__(self, db: AsyncSession, gateway: AsaasGateway) -> None:
        self.db = db
        self.billing = BillingService(db, gateway)

    async def process(self, payload: dict) -> None:
        event_id = payload.get("id")
        event_type = payload.get("event")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > 160
            or not isinstance(event_type, str)
            or not event_type
            or len(event_type) > 80
        ):
            return
        if event_type not in SUPPORTED_EVENTS:
            return

        existing = await self.db.get(BillingWebhookEvent, event_id)
        if existing is not None and existing.processed_at is not None:
            return

        resource_type, resource = self._resource(payload, event_type)
        resource_id = resource.get("id") if isinstance(resource, dict) else None
        if not isinstance(resource_id, str) or len(resource_id) > 100:
            resource_id = None

        if existing is None:
            existing = BillingWebhookEvent(
                event_id=event_id,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            self.db.add(existing)
            await self.db.commit()

        if event_type in CHECKOUT_EVENTS:
            await self._checkout(event_type, resource)
        elif event_type in SUBSCRIPTION_EVENTS:
            await self.billing.apply_subscription_event(event_type, resource)
        elif event_type in PAYMENT_EVENTS:
            await self.billing.apply_payment_event(event_type, resource)

        existing.processed_at = datetime.now(UTC)
        await self.db.commit()

    async def _checkout(self, event_type: str, checkout: dict) -> None:
        provider_id = checkout.get("id")
        if not isinstance(provider_id, str):
            return
        from sqlalchemy import select
        from app.models import BillingCheckout

        local = await self.db.scalar(
            select(BillingCheckout).where(BillingCheckout.provider_checkout_id == provider_id)
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
    def _resource(payload: dict, event_type: str) -> tuple[str, dict]:
        if event_type in CHECKOUT_EVENTS:
            resource = payload.get("checkout")
            return "checkout", resource if isinstance(resource, dict) else {}
        if event_type in SUBSCRIPTION_EVENTS:
            resource = payload.get("subscription")
            return "subscription", resource if isinstance(resource, dict) else {}
        resource = payload.get("payment")
        return "payment", resource if isinstance(resource, dict) else {}

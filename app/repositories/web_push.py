from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuthSession,
    BusinessUserMembership,
    User,
    WebPushDelivery,
    WebPushEvent,
    WebPushSubscription,
)
from app.push.schemas import PushSubscriptionRequest


def push_endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class WebPushRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def register(
        self,
        *,
        user_id: uuid.UUID,
        auth_session_id: uuid.UUID,
        business_id: uuid.UUID,
        payload: PushSubscriptionRequest,
    ) -> None:
        endpoint = str(payload.endpoint)
        endpoint_hash = push_endpoint_hash(endpoint)
        statement = (
            postgresql_insert(WebPushSubscription)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                auth_session_id=auth_session_id,
                business_id=business_id,
                endpoint_hash=endpoint_hash,
                endpoint=endpoint,
                p256dh=payload.keys.p256dh,
                auth_secret=payload.keys.auth,
            )
            .on_conflict_do_update(
                constraint="uq_web_push_subscriptions_business_endpoint",
                set_={
                    "user_id": user_id,
                    "auth_session_id": auth_session_id,
                    "endpoint": endpoint,
                    "p256dh": payload.keys.p256dh,
                    "auth_secret": payload.keys.auth,
                    "updated_at": func.now(),
                },
            )
        )
        await self.session.execute(statement)

    async def remove(
        self,
        *,
        user_id: uuid.UUID,
        auth_session_id: uuid.UUID,
        business_id: uuid.UUID,
        endpoint: str,
    ) -> bool:
        subscription_id = await self.session.scalar(
            select(WebPushSubscription.id).where(
                WebPushSubscription.user_id == user_id,
                WebPushSubscription.auth_session_id == auth_session_id,
                WebPushSubscription.business_id == business_id,
                WebPushSubscription.endpoint_hash == push_endpoint_hash(endpoint),
            )
        )
        if subscription_id is None:
            return False
        await self._delete_subscription(subscription_id)
        return True

    async def remove_for_auth_session(self, auth_session_id: uuid.UUID) -> None:
        subscription_ids = list(
            (
                await self.session.scalars(
                    select(WebPushSubscription.id).where(
                        WebPushSubscription.auth_session_id == auth_session_id
                    )
                )
            ).all()
        )
        if not subscription_ids:
            return
        await self.session.execute(
            delete(WebPushDelivery).where(
                WebPushDelivery.subscription_id.in_(subscription_ids)
            )
        )
        await self.session.execute(
            delete(WebPushSubscription).where(
                WebPushSubscription.id.in_(subscription_ids)
            )
        )

    async def event_for_key(self, event_key: str) -> WebPushEvent | None:
        return await self.session.scalar(
            select(WebPushEvent).where(WebPushEvent.event_key == event_key)
        )

    async def pending_events(
        self, business_id: uuid.UUID
    ) -> list[WebPushEvent]:
        return list(
            (
                await self.session.scalars(
                    select(WebPushEvent)
                    .where(
                        WebPushEvent.business_id == business_id,
                        WebPushEvent.processed_at.is_(None),
                    )
                    .order_by(WebPushEvent.created_at, WebPushEvent.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

    async def active_subscriptions(
        self, business_id: uuid.UUID
    ) -> list[WebPushSubscription]:
        now = datetime.now(UTC)
        return list(
            (
                await self.session.scalars(
                    select(WebPushSubscription)
                    .join(
                        BusinessUserMembership,
                        (
                            BusinessUserMembership.user_id
                            == WebPushSubscription.user_id
                        )
                        & (
                            BusinessUserMembership.business_id
                            == WebPushSubscription.business_id
                        ),
                    )
                    .join(User, User.id == WebPushSubscription.user_id)
                    .join(
                        AuthSession,
                        (AuthSession.id == WebPushSubscription.auth_session_id)
                        & (AuthSession.user_id == WebPushSubscription.user_id),
                    )
                    .where(
                        WebPushSubscription.business_id == business_id,
                        User.is_active.is_(True),
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > now,
                    )
                    .order_by(WebPushSubscription.id)
                )
            ).all()
        )

    async def claim_delivery(
        self,
        subscription: WebPushSubscription,
        event: WebPushEvent,
    ) -> bool:
        now = datetime.now(UTC)
        delivery_id = await self.session.scalar(
            postgresql_insert(WebPushDelivery)
            .values(
                id=uuid.uuid4(),
                business_id=event.business_id,
                subscription_id=subscription.id,
                event_id=event.id,
                status="pending",
                attempts=1,
                attempted_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_web_push_deliveries_subscription_event"
            )
            .returning(WebPushDelivery.id)
        )
        if delivery_id is not None:
            return True

        delivery = await self.session.scalar(
            select(WebPushDelivery)
            .where(
                WebPushDelivery.subscription_id == subscription.id,
                WebPushDelivery.event_id == event.id,
            )
            .with_for_update()
        )
        if delivery is None or delivery.status == "sent":
            return False
        if (
            delivery.status == "pending"
            and delivery.attempted_at > now - timedelta(minutes=5)
        ):
            return False
        delivery.status = "pending"
        delivery.attempts += 1
        delivery.attempted_at = now
        return True

    async def mark_sent(
        self, subscription_id: uuid.UUID, event_id: uuid.UUID
    ) -> None:
        await self.session.execute(
            update(WebPushDelivery)
            .where(
                WebPushDelivery.subscription_id == subscription_id,
                WebPushDelivery.event_id == event_id,
            )
            .values(status="sent", sent_at=func.now(), updated_at=func.now())
        )

    async def mark_failed(
        self, subscription_id: uuid.UUID, event_id: uuid.UUID
    ) -> None:
        await self.session.execute(
            update(WebPushDelivery)
            .where(
                WebPushDelivery.subscription_id == subscription_id,
                WebPushDelivery.event_id == event_id,
            )
            .values(status="failed", updated_at=func.now())
        )

    async def remove_invalid(self, subscription_id: uuid.UUID) -> None:
        await self._delete_subscription(subscription_id)

    async def complete_event(self, event_id: uuid.UUID) -> None:
        await self.session.execute(
            update(WebPushEvent)
            .where(WebPushEvent.id == event_id)
            .values(processed_at=func.now())
        )

    async def commit(self) -> None:
        await self.session.commit()

    async def _delete_subscription(self, subscription_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(WebPushDelivery).where(
                WebPushDelivery.subscription_id == subscription_id
            )
        )
        await self.session.execute(
            delete(WebPushSubscription).where(
                WebPushSubscription.id == subscription_id
            )
        )

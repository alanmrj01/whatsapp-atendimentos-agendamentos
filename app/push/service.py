from __future__ import annotations

import json
import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, WebPushConfigurationError
from app.models import WebPushEvent, WebPushSubscription
from app.push.sender import (
    InvalidPushSubscription,
    PushTarget,
    PyWebPushSender,
    WebPushDeliveryError,
    WebPushSender,
)
from app.repositories.web_push import WebPushRepository

logger = logging.getLogger(__name__)


class WebPushDispatchError(RuntimeError):
    """At least one delivery failed transiently and may be retried."""


class PushRepository(Protocol):
    async def event_for_key(self, event_key: str) -> WebPushEvent | None: ...

    async def pending_events(self, business_id: UUID) -> list[WebPushEvent]: ...

    async def active_subscriptions(
        self, business_id: UUID
    ) -> list[WebPushSubscription]: ...

    async def claim_delivery(
        self, subscription: WebPushSubscription, event: WebPushEvent
    ) -> bool: ...

    async def mark_sent(self, subscription_id: UUID, event_id: UUID) -> None: ...

    async def mark_failed(self, subscription_id: UUID, event_id: UUID) -> None: ...

    async def remove_invalid(self, subscription_id: UUID) -> None: ...

    async def complete_event(self, event_id: UUID) -> None: ...

    async def commit(self) -> None: ...


class WebPushDispatcher:
    def __init__(self, repository: PushRepository, sender: WebPushSender):
        self.repository = repository
        self.sender = sender

    async def dispatch_pending_from(self, event_key: str) -> int:
        trigger = await self.repository.event_for_key(event_key)
        if trigger is None:
            return 0

        sent = 0
        transient_failure = False
        invalid_subscriptions: set[UUID] = set()
        events = await self.repository.pending_events(trigger.business_id)
        subscriptions = await self.repository.active_subscriptions(
            trigger.business_id
        )
        for event in events:
            payload = _payload(event)
            if payload is None:
                await self.repository.complete_event(event.id)
                continue
            for subscription in subscriptions:
                if subscription.id in invalid_subscriptions:
                    continue
                if not await self.repository.claim_delivery(subscription, event):
                    continue
                try:
                    await self.sender.send(
                        PushTarget(
                            endpoint=subscription.endpoint,
                            p256dh=subscription.p256dh,
                            auth=subscription.auth_secret,
                        ),
                        payload,
                    )
                except InvalidPushSubscription:
                    await self.repository.remove_invalid(subscription.id)
                    invalid_subscriptions.add(subscription.id)
                except WebPushDeliveryError:
                    await self.repository.mark_failed(subscription.id, event.id)
                    transient_failure = True
                else:
                    await self.repository.mark_sent(subscription.id, event.id)
                    sent += 1
            if not transient_failure:
                await self.repository.complete_event(event.id)

        await self.repository.commit()
        if transient_failure:
            logger.warning("web_push_delivery_failed")
            raise WebPushDispatchError("Web Push delivery failed")
        if events:
            logger.info(
                "web_push_events_processed",
                extra={"event_count": len(events), "delivery_count": sent},
            )
        return sent


async def dispatch_pending_web_push(
    session: AsyncSession,
    event_key: str,
    settings: Settings,
) -> int:
    try:
        configuration = settings.require_web_push_configuration()
    except WebPushConfigurationError:
        return 0
    return await WebPushDispatcher(
        WebPushRepository(session),
        PyWebPushSender(configuration),
    ).dispatch_pending_from(event_key)


def _payload(event: WebPushEvent) -> str | None:
    action_copy = {
        "human_intervention": (
            "Atendimento precisa de você",
            "Abra a conversa para continuar o atendimento.",
        ),
        "connection_action": (
            "WhatsApp pronto para a próxima etapa",
            "A Meta concluiu uma revisão da conta. Abra a Alovia para tentar ativar o uso conjunto.",
        ),
        "billing_action": (
            "Pagamento precisa de atenção",
            "Abra a Alovia para revisar sua assinatura.",
        ),
    }
    content = action_copy.get(event.event_type)
    if content is None:
        # Incoming messages and automatic bookings stay visible inside Alovia,
        # but they do not interrupt the device unless human action is required.
        return None
    title, body = content
    return json.dumps(
        {
            "type": event.event_type,
            "event_id": str(event.id),
            "title": title,
            "body": body,
            "target_path": event.target_path,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

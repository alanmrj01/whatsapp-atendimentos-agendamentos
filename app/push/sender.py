from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from anyio import to_thread
from pywebpush import WebPushException, webpush

from app.core.config import WebPushConfiguration


@dataclass(frozen=True, slots=True)
class PushTarget:
    endpoint: str
    p256dh: str
    auth: str


class InvalidPushSubscription(RuntimeError):
    """A subscription is no longer accepted by the push service."""


class WebPushDeliveryError(RuntimeError):
    """A transient or unknown Web Push delivery error."""


class WebPushSender(Protocol):
    async def send(self, target: PushTarget, payload: str) -> None: ...


class PyWebPushSender:
    def __init__(self, configuration: WebPushConfiguration):
        self.configuration = configuration

    async def send(self, target: PushTarget, payload: str) -> None:
        def _send() -> None:
            try:
                webpush(
                    subscription_info={
                        "endpoint": target.endpoint,
                        "keys": {
                            "p256dh": target.p256dh,
                            "auth": target.auth,
                        },
                    },
                    data=payload,
                    vapid_private_key=(
                        self.configuration.private_key.get_secret_value()
                    ),
                    vapid_claims={"sub": self.configuration.subject},
                    ttl=300,
                )
            except WebPushException as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code in {404, 410}:
                    raise InvalidPushSubscription from None
                raise WebPushDeliveryError from None
            except Exception:
                raise WebPushDeliveryError from None

        await to_thread.run_sync(_send)

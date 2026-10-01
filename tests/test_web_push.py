from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import web_push as web_push_api
from app.auth.schemas import MembershipResponse, MembershipRole
from app.auth.service import Principal
from app.core.config import Settings, WebPushConfigurationError
from app.models import AuthSession, User, WebPushEvent, WebPushSubscription
from app.push.schemas import PushSubscriptionRequest
from app.push.sender import (
    InvalidPushSubscription,
    WebPushDeliveryError,
)
from app.push.service import WebPushDispatcher, WebPushDispatchError
from app.repositories.whatsapp_webhook import WhatsAppWebhookRepository
from app.whatsapp.webhook import InboundMessageEvent


BUSINESS_A = uuid4()
BUSINESS_B = uuid4()
USER_ID = uuid4()
SESSION_ID = uuid4()


def subscription(business_id=BUSINESS_A) -> WebPushSubscription:
    return WebPushSubscription(
        id=uuid4(),
        user_id=USER_ID,
        auth_session_id=SESSION_ID,
        business_id=business_id,
        endpoint_hash="a" * 64,
        endpoint="https://push.example.test/subscription",
        p256dh="p256dh-value",
        auth_secret="auth-value",
    )


def push_event(event_type="inbound_message") -> WebPushEvent:
    return WebPushEvent(
        id=uuid4(),
        business_id=BUSINESS_A,
        event_key="inbound:provider-id",
        event_type=event_type,
        target_path=(
            f"/app/conversas/{uuid4()}"
            if event_type == "inbound_message"
            else "/app/agenda"
        ),
    )


class FakeRepository:
    def __init__(self, event, subscriptions):
        self.event = event
        self.subscriptions = subscriptions
        self.claimed = set()
        self.sent = []
        self.failed = []
        self.invalid = []
        self.completed = []
        self.commits = 0

    async def event_for_key(self, event_key):
        return self.event if event_key == self.event.event_key else None

    async def pending_events(self, business_id):
        return [] if self.event.id in self.completed else [self.event]

    async def active_subscriptions(self, business_id):
        return [item for item in self.subscriptions if item.business_id == business_id]

    async def claim_delivery(self, target, event):
        key = (target.id, event.id)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def mark_sent(self, subscription_id, event_id):
        self.sent.append((subscription_id, event_id))

    async def mark_failed(self, subscription_id, event_id):
        self.failed.append((subscription_id, event_id))

    async def remove_invalid(self, subscription_id):
        self.invalid.append(subscription_id)

    async def complete_event(self, event_id):
        self.completed.append(event_id)

    async def commit(self):
        self.commits += 1


class RecordingSender:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send(self, target, payload):
        self.calls.append((target, json.loads(payload)))
        if self.error:
            raise self.error


@pytest.mark.asyncio
async def test_dispatch_is_tenant_scoped_deduplicated_and_payload_is_safe():
    event = push_event()
    tenant_a = subscription(BUSINESS_A)
    tenant_b = subscription(BUSINESS_B)
    repository = FakeRepository(event, [tenant_a, tenant_b])
    sender = RecordingSender()
    dispatcher = WebPushDispatcher(repository, sender)

    assert await dispatcher.dispatch_pending_from(event.event_key) == 1
    assert await dispatcher.dispatch_pending_from(event.event_key) == 0

    assert len(sender.calls) == 1
    payload = sender.calls[0][1]
    assert payload["target_path"] == event.target_path
    assert payload["body"] == "Você recebeu uma nova mensagem."
    assert "phone" not in json.dumps(payload).casefold()
    assert "message_body" not in payload
    assert repository.completed == [event.id]


@pytest.mark.asyncio
async def test_invalid_subscription_is_removed_without_retrying_delivery():
    event = push_event()
    target = subscription()
    repository = FakeRepository(event, [target])
    sender = RecordingSender(InvalidPushSubscription())

    assert await WebPushDispatcher(repository, sender).dispatch_pending_from(
        event.event_key
    ) == 0
    assert repository.invalid == [target.id]
    assert repository.failed == []
    assert repository.completed == [event.id]


@pytest.mark.asyncio
async def test_transient_push_failure_stays_retryable():
    event = push_event("automatic_booking")
    target = subscription()
    repository = FakeRepository(event, [target])
    sender = RecordingSender(WebPushDeliveryError())

    with pytest.raises(WebPushDispatchError):
        await WebPushDispatcher(repository, sender).dispatch_pending_from(
            event.event_key
        )

    assert repository.failed == [(target.id, event.id)]
    assert repository.completed == []
    assert repository.commits == 1


def principal(*, business_id=BUSINESS_A, access_mode="paid") -> Principal:
    user = User(
        id=USER_ID,
        email="owner@example.test",
        password_hash="$argon2id$placeholder",
        is_active=True,
    )
    auth_session = AuthSession(
        id=SESSION_ID,
        user_id=USER_ID,
        active_business_id=business_id,
        refresh_token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    membership = MembershipResponse(
        business_id=business_id,
        business_name="Empresa",
        role=MembershipRole.OWNER,
        access_mode=access_mode,
        has_had_operational_access=False,
    )
    return Principal(user=user, session=auth_session, memberships=[membership])


def settings() -> Settings:
    return Settings(
        ENVIRONMENT="test",
        WEB_PUSH_ENABLED=True,
        VAPID_PUBLIC_KEY="A" * 87,
        VAPID_PRIVATE_KEY="private-key",
        VAPID_SUBJECT="mailto:operations@example.test",
    )


def test_web_push_is_fail_closed_without_complete_backend_configuration():
    disabled = Settings(ENVIRONMENT="test")
    assert disabled.web_push_enabled is False
    with pytest.raises(WebPushConfigurationError, match="disabled"):
        disabled.require_web_push_configuration()

    incomplete = Settings(ENVIRONMENT="test", WEB_PUSH_ENABLED=True)
    with pytest.raises(WebPushConfigurationError, match="incomplete"):
        incomplete.require_web_push_configuration()


@pytest.mark.asyncio
async def test_web_push_config_exposes_only_public_key():
    response = await web_push_api.web_push_config(principal(), settings())
    assert response.enabled is True
    assert response.public_key == "A" * 87
    serialized = response.model_dump_json()
    assert "private-key" not in serialized
    assert "operations@example.test" not in serialized


@pytest.mark.asyncio
async def test_subscription_registration_uses_authenticated_user_session_and_tenant(
    monkeypatch,
):
    captured = {}

    class FakeWebPushRepository:
        def __init__(self, _db):
            pass

        async def register(self, **values):
            captured.update(values)

    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(web_push_api, "WebPushRepository", FakeWebPushRepository)
    payload = PushSubscriptionRequest.model_validate(
        {
            "endpoint": "https://push.example.test/subscription",
            "keys": {"p256dh": "p" * 32, "auth": "a" * 16},
        }
    )

    result = await web_push_api.register_web_push_subscription(
        payload,
        principal(),
        settings(),
        db,
    )

    assert result.subscribed is True
    assert captured["business_id"] == BUSINESS_A
    assert captured["user_id"] == USER_ID
    assert captured["auth_session_id"] == SESSION_ID
    assert "business_id" not in payload.model_dump()


def test_never_activated_free_tenant_cannot_register_push_subscription():
    with pytest.raises(HTTPException) as exc:
        web_push_api._operational_business(principal(access_mode="free"))
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_persisted_inbound_creates_content_free_push_event(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock())
    outreach = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.whatsapp_webhook.mark_outreach_response",
        outreach,
    )
    conversation_id = uuid4()
    event = InboundMessageEvent(
        event_key="inbound:provider-id",
        event_type="message.inbound.text",
        meta_phone_number_id="phone-id",
        provider_message_id="provider-id",
        whatsapp_id="5511999999999",
        message_type="text",
        body="conteúdo privado",
        interactive_id=None,
    )

    await WhatsAppWebhookRepository(session).persist_inbound_message(
        BUSINESS_A,
        conversation_id,
        event,
    )

    assert session.execute.await_count == 2
    push_statement = session.execute.await_args_list[1].args[0]
    parameters = push_statement.compile().params
    assert parameters["event_type"] == "inbound_message"
    assert parameters["event_key"] == event.event_key
    assert parameters["target_path"] == f"/app/conversas/{conversation_id}"
    assert "conteúdo privado" not in parameters.values()

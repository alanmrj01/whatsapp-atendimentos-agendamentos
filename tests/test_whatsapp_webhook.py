from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock

from httpx import AsyncClient
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.exc import IntegrityError

from app.api import whatsapp_webhook as webhook_api
from app.repositories.whatsapp_webhook import build_claim_event_statement
from app.whatsapp.processor import process_webhook_events
from app.whatsapp.webhook import (
    InboundMessageEvent,
    MessageStatusEvent,
    NormalizedWebhookEvent,
    build_event_key,
)


APP_SECRET = "test-app-secret"


def encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def signature_for(body: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def messages_payload(
    *,
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    phone_number_id: str = "known-phone-id",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": phone_number_id},
    }
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "test-waba-id",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }


async def post_signed(
    client: AsyncClient, payload: dict[str, Any]
) -> tuple[bytes, Any]:
    body = encode_payload(payload)
    response = await client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(body),
        },
    )
    return body, response


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeSession:
    def begin(self) -> FakeTransaction:
        return FakeTransaction()


class FakeConstraintError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("integrity error")
        self.constraint_name = constraint_name


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.business_id = uuid.UUID(int=1)
        self.customer_id = uuid.UUID(int=2)
        self.conversation_id = uuid.UUID(int=3)
        self.claimed: set[str] = set()
        self.claim_lock = asyncio.Lock()
        self.persisted_messages: list[InboundMessageEvent] = []
        self.status_updates: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.customer_lookups: list[str] = []
        self.integrity_constraint_name: str | None = None

    async def claim_event(self, event: NormalizedWebhookEvent) -> bool:
        if self.integrity_constraint_name is not None:
            original_error = FakeConstraintError(self.integrity_constraint_name)
            raise IntegrityError("INSERT", {}, original_error)
        async with self.claim_lock:
            if event.event_key in self.claimed:
                return False
            self.claimed.add(event.event_key)
            return True

    async def find_business_id(
        self, meta_phone_number_id: str
    ) -> uuid.UUID | None:
        if meta_phone_number_id == "unknown-phone-id":
            return None
        return self.business_id

    async def get_or_create_customer_id(
        self, _: uuid.UUID, whatsapp_id: str
    ) -> uuid.UUID:
        self.customer_lookups.append(whatsapp_id)
        return self.customer_id

    async def get_or_create_conversation_id(
        self, _: uuid.UUID, customer_id: uuid.UUID
    ) -> uuid.UUID:
        assert customer_id == self.customer_id
        return self.conversation_id

    async def touch_conversation(self, conversation_id: uuid.UUID) -> None:
        assert conversation_id == self.conversation_id

    async def persist_inbound_message(
        self,
        _: uuid.UUID,
        conversation_id: uuid.UUID,
        event: InboundMessageEvent,
    ) -> None:
        assert conversation_id == self.conversation_id
        self.persisted_messages.append(event)

    async def update_message_status(
        self,
        _: uuid.UUID,
        provider_message_id: str,
        message_status: str,
    ) -> None:
        self.status_updates.append((provider_message_id, message_status))

    async def complete_event(self, event_key: str, event_status: str) -> None:
        self.completed.append((event_key, event_status))


@mark.asyncio
async def test_webhook_verification_with_correct_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge-value",
        },
    )

    assert response.status_code == 200
    assert response.text == "challenge-value"


@mark.asyncio
async def test_webhook_verification_with_incorrect_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "incorrect-token",
            "hub.challenge": "challenge-value",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Webhook verification failed"}


@mark.asyncio
async def test_webhook_verification_with_invalid_parameters(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid request"


@mark.asyncio
async def test_post_accepts_valid_signature(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    payload = messages_payload(
        messages=[
            {
                "id": "provider-message-1",
                "from": "customer-1",
                "type": "text",
                "text": {"body": "hello"},
            }
        ]
    )

    _, response = await post_signed(client, payload)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    processor.assert_awaited_once()


@mark.asyncio
async def test_post_rejects_invalid_signature_without_persistence(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    body = encode_payload(messages_payload(messages=[]))

    response = await client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    processor.assert_not_awaited()


@mark.asyncio
async def test_post_rejects_missing_signature_without_persistence(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    body = encode_payload(messages_payload(messages=[]))

    response = await client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    processor.assert_not_awaited()


@mark.asyncio
async def test_post_rejects_tampered_body(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    original_body = encode_payload(messages_payload(messages=[]))
    tampered_body = original_body.replace(b'"entry"', b'"entries"')

    response = await client.post(
        "/webhook/whatsapp",
        content=tampered_body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(original_body),
        },
    )

    assert response.status_code == 401
    processor.assert_not_awaited()


@mark.asyncio
async def test_post_normalizes_inbound_text(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    payload = messages_payload(
        messages=[
            {
                "id": "provider-text-1",
                "from": "customer-text-1",
                "type": "text",
                "text": {"body": "text content"},
            }
        ]
    )

    _, response = await post_signed(client, payload)
    events = processor.await_args.args[1]

    assert response.status_code == 200
    assert events == [
        InboundMessageEvent(
            event_key=build_event_key("inbound", "provider-text-1"),
            event_type="message.inbound.text",
            meta_phone_number_id="known-phone-id",
            provider_message_id="provider-text-1",
            whatsapp_id="customer-text-1",
            message_type="text",
            body="text content",
            interactive_id=None,
        )
    ]


@mark.asyncio
async def test_post_normalizes_interactive_reply(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    payload = messages_payload(
        messages=[
            {
                "id": "provider-interactive-1",
                "from": "customer-interactive-1",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "option-1", "title": "Option"},
                },
            }
        ]
    )

    _, response = await post_signed(client, payload)
    event = processor.await_args.args[1][0]

    assert response.status_code == 200
    assert isinstance(event, InboundMessageEvent)
    assert event.message_type == "interactive"
    assert event.body == "Option"
    assert event.interactive_id == "option-1"


@mark.asyncio
async def test_post_supports_multiple_entries_changes_and_messages(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    first_value = messages_payload(
        messages=[
            {
                "id": "provider-multi-1",
                "from": "customer-multi",
                "type": "text",
                "text": {"body": "first"},
            },
            {
                "id": "provider-multi-2",
                "from": "customer-multi",
                "type": "text",
                "text": {"body": "second"},
            },
        ]
    )["entry"][0]["changes"][0]["value"]
    second_value = messages_payload(
        messages=[
            {
                "id": "provider-multi-3",
                "from": "customer-multi",
                "type": "text",
                "text": {"body": "third"},
            }
        ]
    )["entry"][0]["changes"][0]["value"]
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {"field": "messages", "value": first_value},
                    {"field": "unsupported", "value": {}},
                ]
            },
            {"changes": [{"field": "messages", "value": second_value}]},
        ],
    }

    _, response = await post_signed(client, payload)
    events = processor.await_args.args[1]

    assert response.status_code == 200
    assert [event.provider_message_id for event in events] == [
        "provider-multi-1",
        "provider-multi-2",
        "provider-multi-3",
    ]


@mark.asyncio
async def test_post_normalizes_distinct_message_statuses(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    payload = messages_payload(
        statuses=[
            {"id": "provider-status-1", "status": message_status}
            for message_status in ("sent", "delivered", "read", "failed")
        ]
    )

    _, response = await post_signed(client, payload)
    events = processor.await_args.args[1]

    assert response.status_code == 200
    assert all(isinstance(event, MessageStatusEvent) for event in events)
    assert [event.message_status for event in events] == [
        "sent",
        "delivered",
        "read",
        "failed",
    ]
    assert len({event.event_key for event in events}) == 4


@mark.asyncio
async def test_sequential_duplicate_is_processed_once() -> None:
    repository = FakeWebhookRepository()
    event = InboundMessageEvent(
        event_key=build_event_key("inbound", "provider-duplicate-1"),
        event_type="message.inbound.text",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-duplicate-1",
        whatsapp_id="customer-duplicate-1",
        message_type="text",
        body="duplicate body",
        interactive_id=None,
    )

    await process_webhook_events(FakeSession(), [event], repository)
    await process_webhook_events(FakeSession(), [event], repository)

    assert len(repository.persisted_messages) == 1
    assert repository.completed == [(event.event_key, "processed")]


@mark.asyncio
async def test_concurrent_duplicate_is_processed_once() -> None:
    repository = FakeWebhookRepository()
    event = InboundMessageEvent(
        event_key=build_event_key("inbound", "provider-concurrent-1"),
        event_type="message.inbound.text",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-concurrent-1",
        whatsapp_id="customer-concurrent-1",
        message_type="text",
        body=None,
        interactive_id=None,
    )

    await asyncio.gather(
        process_webhook_events(FakeSession(), [event], repository),
        process_webhook_events(FakeSession(), [event], repository),
    )

    assert len(repository.persisted_messages) == 1
    assert repository.completed == [(event.event_key, "processed")]


@mark.asyncio
async def test_integrity_error_is_treated_as_valid_duplicate() -> None:
    repository = FakeWebhookRepository()
    repository.integrity_constraint_name = "uq_processed_webhooks_event_key"
    event = MessageStatusEvent(
        event_key=build_event_key("status", "provider-race-1", "delivered"),
        event_type="message.status.delivered",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-race-1",
        message_status="delivered",
    )

    await process_webhook_events(FakeSession(), [event], repository)

    assert repository.status_updates == []
    assert repository.completed == []


@mark.asyncio
async def test_unrelated_integrity_error_is_not_masked_as_duplicate() -> None:
    repository = FakeWebhookRepository()
    repository.integrity_constraint_name = "fk_messages_business_id"
    event = MessageStatusEvent(
        event_key=build_event_key("status", "provider-error-1", "delivered"),
        event_type="message.status.delivered",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-error-1",
        message_status="delivered",
    )

    with raises(IntegrityError):
        await process_webhook_events(FakeSession(), [event], repository)


@mark.asyncio
async def test_status_event_updates_matching_message() -> None:
    repository = FakeWebhookRepository()
    event = MessageStatusEvent(
        event_key=build_event_key("status", "provider-status-2", "read"),
        event_type="message.status.read",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-status-2",
        message_status="read",
    )

    await process_webhook_events(FakeSession(), [event], repository)

    assert repository.status_updates == [("provider-status-2", "read")]
    assert repository.completed == [(event.event_key, "processed")]


@mark.asyncio
async def test_unknown_business_is_safely_ignored(
    caplog: LogCaptureFixture,
) -> None:
    repository = FakeWebhookRepository()
    event = InboundMessageEvent(
        event_key=build_event_key("inbound", "provider-unknown-business"),
        event_type="message.inbound.text",
        meta_phone_number_id="unknown-phone-id",
        provider_message_id="provider-unknown-business",
        whatsapp_id="unknown-customer",
        message_type="text",
        body="not persisted",
        interactive_id=None,
    )

    caplog.set_level(logging.INFO)
    await process_webhook_events(FakeSession(), [event], repository)

    assert repository.persisted_messages == []
    assert repository.customer_lookups == []
    assert repository.completed == [(event.event_key, "ignored")]
    for sensitive_value in (
        event.meta_phone_number_id,
        event.provider_message_id,
        event.whatsapp_id,
        event.body,
    ):
        assert sensitive_value not in caplog.text


@mark.asyncio
async def test_unknown_event_returns_safe_acknowledgement(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "account_update", "value": {}}]}],
    }

    _, response = await post_signed(client, payload)

    assert response.status_code == 200
    assert processor.await_args.args[1] == []


@mark.asyncio
async def test_invalid_payload_returns_422_without_processing(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    body = b'{"object":"whatsapp_business_account","entry":'

    response = await client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(body),
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request"}
    processor.assert_not_awaited()


@mark.asyncio
async def test_logs_do_not_include_webhook_pii(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    processor = AsyncMock()
    monkeypatch.setattr(webhook_api, "process_webhook_events", processor)
    caplog.set_level(logging.INFO)
    payload = messages_payload(
        phone_number_id="sensitive-phone-id",
        messages=[
            {
                "id": "sensitive-provider-id",
                "from": "sensitive-customer-id",
                "type": "text",
                "text": {"body": "sensitive-message-body"},
            }
        ],
    )

    body, response = await post_signed(client, payload)
    signature = signature_for(body)

    assert response.status_code == 200
    for sensitive_value in (
        "sensitive-phone-id",
        "sensitive-provider-id",
        "sensitive-customer-id",
        "sensitive-message-body",
        signature,
        APP_SECRET,
    ):
        assert sensitive_value not in caplog.text


def test_processed_webhook_claim_uses_postgresql_unique_barrier() -> None:
    event = MessageStatusEvent(
        event_key=build_event_key("status", "provider-sql-1", "delivered"),
        event_type="message.status.delivered",
        meta_phone_number_id="known-phone-id",
        provider_message_id="provider-sql-1",
        message_status="delivered",
    )

    sql = str(
        build_claim_event_statement(event).compile(dialect=postgresql_dialect())
    )

    assert (
        "ON CONFLICT ON CONSTRAINT uq_processed_webhooks_event_key DO NOTHING"
        in sql
    )
    assert "RETURNING processed_webhooks.id" in sql

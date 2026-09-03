from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists
from httpx import AsyncClient
from pydantic import ValidationError
from pytest import LogCaptureFixture, MonkeyPatch

from app.api import internal_tasks
from app.core.config import CloudTasksConfigurationError, Settings
from app.main import app
from app.repositories.outbound_tasks import StoredOutboundMessage
from app.schemas.cloud_tasks import WhatsAppOutboundTaskPayload
from app.tasks import auth as task_auth
from app.tasks.auth import require_cloud_tasks_oidc, require_outbound_tasks_oidc
from app.tasks.cloud_tasks import (
    CloudTasksOutboundEnqueuer,
    deterministic_outbound_task_id,
)
from app.tasks.outbound import (
    OutboundTaskTransientError,
    enqueue_pending_outbounds_for_events,
    process_outbound_message,
)
from app.whatsapp.client import (
    WhatsAppConfigurationError,
    WhatsAppPermanentError,
    WhatsAppRateLimitError,
    WhatsAppTemporaryError,
    WhatsAppTimeoutError,
)
from app.whatsapp.webhook import InboundMessageEvent, build_event_key

INVOKER_EMAIL = (
    "whatsapp-task-invoker@whatsapp-automacao-prod.iam.gserviceaccount.com"
)
MESSAGE_ID = uuid.UUID("70000000-0000-0000-0000-000000000007")
BUSINESS_ID = uuid.UUID("80000000-0000-0000-0000-000000000008")


def outbound_settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "cloud_tasks_enabled": False,
            "outbound_tasks_enabled": True,
            "gcp_project_id": "whatsapp-automacao-prod",
            "gcp_region": "southamerica-east1",
            "cloud_tasks_outbound_queue": "whatsapp-outbound",
            "cloud_tasks_outbound_target_url": (
                "https://service.example.run.app/internal/tasks/whatsapp-outbound"
            ),
            "cloud_tasks_oidc_audience": "https://service.example.run.app",
            "cloud_tasks_invoker_email": INVOKER_EMAIL,
        }
    )


def inbound_and_outbound_settings() -> Settings:
    return outbound_settings().model_copy(
        update={
            "cloud_tasks_enabled": True,
            "cloud_tasks_events_queue": "whatsapp-events",
            "cloud_tasks_target_url": (
                "https://service.example.run.app/internal/tasks/whatsapp-event"
            ),
        }
    )


def test_outbound_tasks_are_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.outbound_tasks_enabled is False
    assert settings.cloud_tasks_outbound_queue == "whatsapp-outbound"
    with pytest.raises(CloudTasksConfigurationError, match="disabled"):
        settings.require_outbound_tasks_configuration()


def test_outbound_tasks_require_target_but_not_inbound_feature() -> None:
    settings = outbound_settings().model_copy(
        update={"cloud_tasks_outbound_target_url": None}
    )

    with pytest.raises(CloudTasksConfigurationError, match="incomplete"):
        settings.require_outbound_tasks_configuration()

    configuration = outbound_settings().require_outbound_tasks_configuration()
    assert configuration.queue == "whatsapp-outbound"
    assert configuration.invoker_email == INVOKER_EMAIL


class FakeCloudTasksClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []

    async def create_task(self, request: Any) -> None:
        self.requests.append(request)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_outbound_enqueue_is_deterministic_minimal_and_uses_oidc() -> None:
    client = FakeCloudTasksClient()
    enqueuer = CloudTasksOutboundEnqueuer(
        outbound_settings().require_outbound_tasks_configuration(),
        client=client,  # type: ignore[arg-type]
    )

    await enqueuer.enqueue(MESSAGE_ID)

    request = client.requests[0]
    task = request.task
    assert task.name.endswith(
        f"/tasks/{deterministic_outbound_task_id(MESSAGE_ID)}"
    )
    assert "/queues/whatsapp-outbound/" in task.name
    assert json.loads(task.http_request.body) == {"message_id": str(MESSAGE_ID)}
    assert set(json.loads(task.http_request.body)) == {"message_id"}
    assert task.http_request.oidc_token.audience == (
        "https://service.example.run.app"
    )
    assert task.http_request.oidc_token.service_account_email == INVOKER_EMAIL
    assert str(MESSAGE_ID) not in task.name


@pytest.mark.asyncio
async def test_outbound_enqueue_treats_already_exists_as_success() -> None:
    enqueuer = CloudTasksOutboundEnqueuer(
        outbound_settings().require_outbound_tasks_configuration(),
        client=FakeCloudTasksClient(AlreadyExists("exists")),  # type: ignore[arg-type]
    )

    await enqueuer.enqueue(MESSAGE_ID)


@pytest.mark.asyncio
async def test_outbound_oidc_reuses_audience_and_identity(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def verify(token: str, audience: str) -> dict[str, Any]:
        captured.update(token=token, audience=audience)
        return {"email": INVOKER_EMAIL, "email_verified": True}

    monkeypatch.setattr(task_auth, "_verify_google_oidc_token", verify)

    await require_outbound_tasks_oidc(
        outbound_settings(),
        "Bearer signed-id-token",
    )

    assert captured == {
        "token": "signed-id-token",
        "audience": "https://service.example.run.app",
    }


@pytest.mark.asyncio
async def test_outbound_oidc_is_fail_closed_when_disabled() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_outbound_tasks_oidc(Settings(_env_file=None), None)

    assert exc_info.value.status_code == 503


class FakeSession:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


class FakeOutboundRepository:
    def __init__(self, message: StoredOutboundMessage | None) -> None:
        self.message = message
        self.sent: list[tuple[uuid.UUID, str]] = []
        self.failed: list[uuid.UUID] = []

    async def lock_message(
        self, _: uuid.UUID
    ) -> StoredOutboundMessage | None:
        return self.message

    async def mark_sent(
        self, message_id: uuid.UUID, provider_message_id: str
    ) -> None:
        self.sent.append((message_id, provider_message_id))

    async def mark_failed(self, message_id: uuid.UUID) -> None:
        self.failed.append(message_id)


class FakeWhatsAppSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, Any]] = []
        self.closed = 0

    async def send_text(self, to: str, text: str) -> str:
        return await self._send("text", (to, text))

    async def send_interactive_buttons(
        self, to: str, body: str, buttons: Any
    ) -> str:
        return await self._send("buttons", (to, body, buttons))

    async def send_interactive_list(
        self, to: str, body: str, sections: Any
    ) -> str:
        return await self._send("list", (to, body, sections))

    async def _send(self, kind: str, arguments: Any) -> str:
        self.calls.append((kind, arguments))
        if self.error is not None:
            raise self.error
        return "wamid.outbound"

    async def aclose(self) -> None:
        self.closed += 1


class FakeBusinessSenderResolver:
    def __init__(self, sender: FakeWhatsAppSender) -> None:
        self.sender = sender
        self.business_ids: list[uuid.UUID] = []

    async def resolve(self, business_id: uuid.UUID) -> FakeWhatsAppSender:
        self.business_ids.append(business_id)
        return self.sender


def stored_message(
    *,
    message_type: str = "text",
    status: str = "pending",
    recipient: str = "5511999990001",
    automation_blocked: bool = False,
) -> StoredOutboundMessage:
    payloads = {
        "interactive_button": {
            "buttons": [{"id": "yes", "title": "Sim"}]
        },
        "interactive_list": {
            "button": "Ver opções",
            "sections": [
                {
                    "title": "Serviços",
                    "rows": [{"id": "service:1", "title": "Serviço"}],
                }
            ],
        },
    }
    return StoredOutboundMessage(
        message_id=MESSAGE_ID,
        business_id=BUSINESS_ID,
        recipient=recipient,
        message_type=message_type,
        body="Conteúdo de teste",
        outbound_payload=payloads.get(message_type),
        status=status,
        provider_message_id=None,
        automation_blocked=automation_blocked,
    )


@pytest.mark.parametrize(
    ("message_type", "expected_call"),
    [
        ("text", "text"),
        ("interactive_button", "buttons"),
        ("interactive_list", "list"),
    ],
)
@pytest.mark.asyncio
async def test_sender_supports_outbound_payload_types(
    message_type: str,
    expected_call: str,
) -> None:
    repository = FakeOutboundRepository(stored_message(message_type=message_type))
    sender = FakeWhatsAppSender()

    result = await process_outbound_message(
        FakeSession(),
        MESSAGE_ID,
        lambda: sender,
        repository,
    )

    assert result == "sent"
    assert sender.calls[0][0] == expected_call
    assert sender.closed == 1
    assert repository.sent == [(MESSAGE_ID, "wamid.outbound")]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_outbound_resolves_sender_from_message_business() -> None:
    repository = FakeOutboundRepository(stored_message())
    sender = FakeWhatsAppSender()
    resolver = FakeBusinessSenderResolver(sender)

    result = await process_outbound_message(
        FakeSession(),
        MESSAGE_ID,
        repository=repository,
        sender_resolver=resolver,
    )

    assert result == "sent"
    assert resolver.business_ids == [BUSINESS_ID]
    assert sender.calls[0][0] == "text"


@pytest.mark.parametrize("terminal_status", ["sent", "delivered", "read", "failed"])
@pytest.mark.asyncio
async def test_terminal_statuses_are_never_resent(terminal_status: str) -> None:
    repository = FakeOutboundRepository(
        stored_message(status=terminal_status)
    )
    factory = AsyncMock()

    result = await process_outbound_message(
        FakeSession(),
        MESSAGE_ID,
        factory,
        repository,
    )

    assert result == "skipped"
    factory.assert_not_called()
    assert repository.sent == []


@pytest.mark.parametrize(
    "error",
    [
        WhatsAppTimeoutError("timeout"),
        WhatsAppRateLimitError("rate limit"),
        WhatsAppTemporaryError("server unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_transient_failures_remain_pending_for_cloud_tasks_retry(
    error: Exception,
) -> None:
    repository = FakeOutboundRepository(stored_message())
    sender = FakeWhatsAppSender(error)

    with pytest.raises(OutboundTaskTransientError):
        await process_outbound_message(
            FakeSession(),
            MESSAGE_ID,
            lambda: sender,
            repository,
        )

    assert repository.sent == []
    assert repository.failed == []


@pytest.mark.asyncio
async def test_permanent_4xx_marks_message_failed() -> None:
    repository = FakeOutboundRepository(stored_message())
    sender = FakeWhatsAppSender(WhatsAppPermanentError("rejected"))

    result = await process_outbound_message(
        FakeSession(), MESSAGE_ID, lambda: sender, repository
    )

    assert result == "failed"
    assert repository.failed == [MESSAGE_ID]
    assert repository.sent == []


@pytest.mark.asyncio
async def test_missing_meta_configuration_is_retryable_not_failed() -> None:
    repository = FakeOutboundRepository(stored_message())

    def missing_configuration():  # type: ignore[no-untyped-def]
        raise WhatsAppConfigurationError("sensitive configuration detail")

    with pytest.raises(OutboundTaskTransientError) as exc_info:
        await process_outbound_message(
            FakeSession(),
            MESSAGE_ID,
            missing_configuration,
            repository,
        )

    assert "sensitive configuration detail" not in str(exc_info.value)
    assert repository.failed == []


@pytest.mark.asyncio
async def test_collective_recipient_is_failed_without_external_call() -> None:
    repository = FakeOutboundRepository(
        stored_message(recipient="120363000000000000@g.us")
    )
    factory = AsyncMock()

    result = await process_outbound_message(
        FakeSession(),
        MESSAGE_ID,
        factory,
        repository,
    )

    assert result == "failed"
    factory.assert_not_called()
    assert repository.failed == [MESSAGE_ID]


@pytest.mark.asyncio
async def test_excluded_or_human_controlled_recipient_is_not_sent() -> None:
    repository = FakeOutboundRepository(
        stored_message(automation_blocked=True)
    )
    factory = AsyncMock()

    result = await process_outbound_message(
        FakeSession(),
        MESSAGE_ID,
        factory,
        repository,
    )

    assert result == "failed"
    factory.assert_not_called()
    assert repository.failed == [MESSAGE_ID]


class FakePendingRepository:
    def __init__(self, message_ids: list[uuid.UUID]) -> None:
        self.message_ids = message_ids
        self.provider_message_ids: list[str] = []

    async def list_pending_for_provider_message_ids(
        self, provider_message_ids: list[str]
    ) -> list[uuid.UUID]:
        self.provider_message_ids.extend(provider_message_ids)
        return self.message_ids

    async def list_pending_for_event_key(self, _: str) -> list[uuid.UUID]:
        return self.message_ids


class FakeOutboundEnqueuer:
    def __init__(self) -> None:
        self.message_ids: list[uuid.UUID] = []

    async def enqueue(self, message_id: uuid.UUID) -> None:
        self.message_ids.append(message_id)


@pytest.mark.asyncio
async def test_post_commit_discovery_enqueues_pending_and_blocks_groups() -> None:
    repository = FakePendingRepository([MESSAGE_ID, MESSAGE_ID])
    enqueuer = FakeOutboundEnqueuer()
    individual = InboundMessageEvent(
        event_key=build_event_key("inbound", "provider-individual"),
        event_type="message.inbound.text",
        meta_phone_number_id="phone-id",
        provider_message_id="provider-individual",
        whatsapp_id="5511999990001",
        message_type="text",
        body="individual",
        interactive_id=None,
    )
    group = InboundMessageEvent(
        event_key=build_event_key("inbound", "provider-group"),
        event_type="message.inbound.text",
        meta_phone_number_id="phone-id",
        provider_message_id="provider-group",
        whatsapp_id="120363000000000000@g.us",
        message_type="text",
        body="group",
        interactive_id=None,
    )

    found = await enqueue_pending_outbounds_for_events(
        FakeSession(),
        [individual, group],
        enqueuer,
        repository,
    )

    assert found == [MESSAGE_ID, MESSAGE_ID]
    assert repository.provider_message_ids == ["provider-individual"]
    assert enqueuer.message_ids == [MESSAGE_ID]


@pytest.mark.asyncio
async def test_outbound_endpoint_returns_retryable_status_without_pii_in_logs(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    sensitive_values = (
        "5511999999999",
        "private message body",
        "private-access-token",
    )
    worker = AsyncMock(
        side_effect=RuntimeError(" ".join(sensitive_values))
    )
    monkeypatch.setattr(internal_tasks, "process_outbound_message", worker)
    app.dependency_overrides[require_outbound_tasks_oidc] = lambda: None
    app.dependency_overrides[internal_tasks.get_settings] = outbound_settings
    caplog.set_level(logging.INFO)
    try:
        response = await client.post(
            "/internal/tasks/whatsapp-outbound",
            json={"message_id": str(MESSAGE_ID)},
        )
    finally:
        app.dependency_overrides.pop(require_outbound_tasks_oidc, None)
        app.dependency_overrides.pop(internal_tasks.get_settings, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "Task processing failed"}
    for sensitive_value in sensitive_values:
        assert sensitive_value not in caplog.text


@pytest.mark.asyncio
async def test_inbound_worker_enqueues_pending_outbound_after_processing(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    order: list[str] = []

    async def process(*_: Any, **__: Any) -> bool:
        order.append("inbound_committed")
        return True

    async def enqueue(*_: Any, **__: Any) -> list[uuid.UUID]:
        order.append("outbound_enqueued")
        return [MESSAGE_ID]

    monkeypatch.setattr(internal_tasks, "process_cloud_task_event", process)
    monkeypatch.setattr(
        internal_tasks,
        "build_outbound_task_enqueuer",
        lambda _: object(),
    )
    monkeypatch.setattr(
        internal_tasks,
        "enqueue_pending_outbounds_for_event",
        enqueue,
    )
    app.dependency_overrides[require_cloud_tasks_oidc] = lambda: None
    app.dependency_overrides[internal_tasks.get_settings] = (
        inbound_and_outbound_settings
    )
    try:
        response = await client.post(
            "/internal/tasks/whatsapp-event",
            json={
                "event_key": (
                    "whatsapp:inbound:" + "a" * 64
                )
            },
        )
    finally:
        app.dependency_overrides.pop(require_cloud_tasks_oidc, None)
        app.dependency_overrides.pop(internal_tasks.get_settings, None)

    assert response.status_code == 200
    assert order == ["inbound_committed", "outbound_enqueued"]


def test_outbound_payload_forbids_pii_and_raw_content() -> None:
    with pytest.raises(ValidationError):
        WhatsAppOutboundTaskPayload.model_validate(
            {
                "message_id": str(MESSAGE_ID),
                "phone": "5511999999999",
                "body": "private message body",
            }
        )

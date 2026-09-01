from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import AlreadyExists
from httpx import AsyncClient
from pydantic import ValidationError
from pytest import MonkeyPatch

from app.api import internal_tasks
from app.conversations.types import ConversationInput
from app.core.config import (
    CloudTasksConfiguration,
    CloudTasksConfigurationError,
    Settings,
)
from app.main import app
from app.repositories.cloud_tasks import StoredTaskEvent
from app.schemas.cloud_tasks import WhatsAppEventTaskPayload
from app.tasks import auth as task_auth
from app.tasks import cloud_tasks as cloud_tasks_module
from app.tasks.auth import require_cloud_tasks_oidc
from app.tasks.cloud_tasks import (
    CloudTasksEnqueueError,
    CloudTasksEventEnqueuer,
    close_cloud_tasks_client,
    deterministic_task_id,
)
from app.tasks.worker import process_cloud_task_event
from app.whatsapp.webhook import build_event_key

INVOKER_EMAIL = (
    "whatsapp-task-invoker@whatsapp-automacao-prod.iam.gserviceaccount.com"
)
EVENT_KEY = build_event_key("inbound", "provider-cloud-task")


def enabled_settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "cloud_tasks_enabled": True,
            "gcp_project_id": "whatsapp-automacao-prod",
            "gcp_region": "southamerica-east1",
            "cloud_tasks_events_queue": "whatsapp-events",
            "cloud_tasks_target_url": (
                "https://service.example.run.app/internal/tasks/whatsapp-event"
            ),
            "cloud_tasks_oidc_audience": "https://service.example.run.app",
            "cloud_tasks_invoker_email": INVOKER_EMAIL,
        }
    )


def cloud_tasks_configuration() -> CloudTasksConfiguration:
    return enabled_settings().require_cloud_tasks_configuration()


def test_cloud_tasks_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.cloud_tasks_enabled is False
    with pytest.raises(CloudTasksConfigurationError, match="disabled"):
        settings.require_cloud_tasks_configuration()


def test_enabled_cloud_tasks_requires_complete_configuration() -> None:
    settings = Settings(_env_file=None).model_copy(
        update={"cloud_tasks_enabled": True}
    )

    with pytest.raises(CloudTasksConfigurationError, match="incomplete"):
        settings.require_cloud_tasks_configuration()


def test_cloud_tasks_rejects_non_https_target() -> None:
    settings = enabled_settings().model_copy(
        update={"cloud_tasks_target_url": "http://service.invalid/task"}
    )

    with pytest.raises(CloudTasksConfigurationError, match="invalid"):
        settings.require_cloud_tasks_configuration()


def test_cloud_tasks_configuration_uses_expected_invoker() -> None:
    configuration = cloud_tasks_configuration()

    assert configuration.invoker_email == INVOKER_EMAIL
    assert configuration.target_url.endswith("/internal/tasks/whatsapp-event")


class FakeCloudTasksClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []

    async def create_task(self, request: Any) -> None:
        self.requests.append(request)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_enqueue_uses_deterministic_name_minimal_payload_and_oidc() -> None:
    client = FakeCloudTasksClient()
    enqueuer = CloudTasksEventEnqueuer(
        cloud_tasks_configuration(),
        client=client,  # type: ignore[arg-type]
    )

    await enqueuer.enqueue(EVENT_KEY)

    request = client.requests[0]
    task = request.task
    assert task.name.endswith(f"/tasks/{deterministic_task_id(EVENT_KEY)}")
    assert json.loads(task.http_request.body) == {"event_key": EVENT_KEY}
    assert set(json.loads(task.http_request.body)) == {"event_key"}
    assert task.http_request.url.endswith("/internal/tasks/whatsapp-event")
    assert task.http_request.oidc_token.audience == (
        "https://service.example.run.app"
    )
    assert task.http_request.oidc_token.service_account_email == INVOKER_EMAIL
    assert "provider-cloud-task" not in task.name


@pytest.mark.asyncio
async def test_enqueue_treats_already_exists_as_success() -> None:
    enqueuer = CloudTasksEventEnqueuer(
        cloud_tasks_configuration(),
        client=FakeCloudTasksClient(AlreadyExists("exists")),  # type: ignore[arg-type]
    )

    await enqueuer.enqueue(EVENT_KEY)


@pytest.mark.asyncio
async def test_enqueue_sanitizes_transient_failure() -> None:
    enqueuer = CloudTasksEventEnqueuer(
        cloud_tasks_configuration(),
        client=FakeCloudTasksClient(RuntimeError("sensitive detail")),  # type: ignore[arg-type]
    )

    with pytest.raises(CloudTasksEnqueueError) as exc_info:
        await enqueuer.enqueue(EVENT_KEY)

    assert "sensitive detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_global_cloud_tasks_client_is_closed(
    monkeypatch: MonkeyPatch,
) -> None:
    client = AsyncMock()
    monkeypatch.setattr(cloud_tasks_module, "_cloud_tasks_client", client)

    await close_cloud_tasks_client()

    client.close.assert_called_once_with()
    assert cloud_tasks_module._cloud_tasks_client is None


@pytest.mark.asyncio
async def test_oidc_validates_audience_and_expected_email(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def verify(token: str, audience: str) -> dict[str, Any]:
        captured.update(token=token, audience=audience)
        return {"email": INVOKER_EMAIL, "email_verified": True}

    monkeypatch.setattr(task_auth, "_verify_google_oidc_token", verify)

    await require_cloud_tasks_oidc(
        enabled_settings(),
        "Bearer signed-id-token",
    )

    assert captured == {
        "token": "signed-id-token",
        "audience": "https://service.example.run.app",
    }


@pytest.mark.asyncio
async def test_oidc_rejects_wrong_email(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        task_auth,
        "_verify_google_oidc_token",
        lambda *_: {"email": "other@example.com", "email_verified": True},
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_cloud_tasks_oidc(
            enabled_settings(),
            "Bearer signed-id-token",
        )

    assert getattr(exc_info.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_oidc_rejects_invalid_token(monkeypatch: MonkeyPatch) -> None:
    def invalid_token(*_: Any) -> dict[str, Any]:
        raise ValueError("invalid")

    monkeypatch.setattr(task_auth, "_verify_google_oidc_token", invalid_token)

    with pytest.raises(HTTPException) as exc_info:
        await require_cloud_tasks_oidc(
            enabled_settings(),
            "Bearer invalid-token",
        )

    assert getattr(exc_info.value, "status_code", None) == 401


@pytest.mark.asyncio
async def test_oidc_rejects_missing_authorization() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_cloud_tasks_oidc(enabled_settings(), None)

    assert exc_info.value.status_code == 401


class FakeTaskSession:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


class FakeTaskRepository:
    def __init__(self, event: StoredTaskEvent) -> None:
        self.event = event
        self.attempts = 0
        self.completed: list[tuple[str, str]] = []
        self.status_updates: list[tuple[str, str]] = []
        self.inbound = ConversationInput(
            business_id=uuid.UUID(int=1),
            customer_id=uuid.UUID(int=2),
            conversation_id=uuid.UUID(int=3),
            provider_message_id="provider-cloud-task",
            message_type="text",
            body="hello",
            interactive_id=None,
        )

    async def lock_event(self, _: str) -> StoredTaskEvent:
        return self.event

    async def mark_attempt_started(self, _: str) -> None:
        self.attempts += 1

    async def load_inbound(self, _: str) -> ConversationInput:
        return self.inbound

    async def update_message_status(
        self, provider_message_id: str, message_status: str
    ) -> None:
        self.status_updates.append((provider_message_id, message_status))

    async def complete_event(self, event_key: str, event_status: str) -> None:
        self.completed.append((event_key, event_status))
        self.event = replace(self.event, status=event_status)


def stored_event(
    *,
    event_type: str = "message.inbound.text",
    status: str = "queued",
) -> StoredTaskEvent:
    return StoredTaskEvent(
        event_key=EVENT_KEY,
        event_type=event_type,
        provider_message_id="provider-cloud-task",
        status=status,
    )


@pytest.mark.asyncio
async def test_worker_executes_engine_once_and_marks_processed() -> None:
    repository = FakeTaskRepository(stored_event())
    engine = AsyncMock()
    engine.process.return_value = True

    first = await process_cloud_task_event(
        FakeTaskSession(),
        EVENT_KEY,
        repository,
        engine,
    )
    second = await process_cloud_task_event(
        FakeTaskSession(),
        EVENT_KEY,
        repository,
        engine,
    )

    assert first is True
    assert second is False
    engine.process.assert_awaited_once_with(repository.inbound)
    assert repository.completed == [(EVENT_KEY, "processed")]


@pytest.mark.asyncio
async def test_worker_processes_status_without_conversation_engine() -> None:
    repository = FakeTaskRepository(
        stored_event(event_type="message.status.delivered")
    )
    engine = AsyncMock()

    assert await process_cloud_task_event(
        FakeTaskSession(),
        EVENT_KEY,
        repository,
        engine,
    ) is True

    engine.process.assert_not_awaited()
    assert repository.status_updates == [
        ("provider-cloud-task", "delivered")
    ]


@pytest.mark.asyncio
async def test_worker_propagates_transient_error_for_retry() -> None:
    repository = FakeTaskRepository(stored_event())
    engine = AsyncMock()
    engine.process.side_effect = RuntimeError("temporary database failure")

    with pytest.raises(RuntimeError, match="temporary database failure"):
        await process_cloud_task_event(
            FakeTaskSession(),
            EVENT_KEY,
            repository,
            engine,
        )

    assert repository.completed == []


@pytest.mark.asyncio
async def test_internal_task_returns_2xx_for_processed_event(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    worker = AsyncMock(return_value=False)
    monkeypatch.setattr(internal_tasks, "process_cloud_task_event", worker)
    app.dependency_overrides[require_cloud_tasks_oidc] = lambda: None
    try:
        response = await client.post(
            "/internal/tasks/whatsapp-event",
            json={"event_key": EVENT_KEY},
        )
    finally:
        app.dependency_overrides.pop(require_cloud_tasks_oidc, None)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


@pytest.mark.asyncio
async def test_internal_task_returns_non_2xx_for_transient_error(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    worker = AsyncMock(side_effect=RuntimeError("temporary"))
    monkeypatch.setattr(internal_tasks, "process_cloud_task_event", worker)
    app.dependency_overrides[require_cloud_tasks_oidc] = lambda: None
    try:
        response = await client.post(
            "/internal/tasks/whatsapp-event",
            json={"event_key": EVENT_KEY},
        )
    finally:
        app.dependency_overrides.pop(require_cloud_tasks_oidc, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "Task processing failed"}


def test_task_payload_forbids_raw_or_pii_fields() -> None:
    with pytest.raises(ValidationError):
        WhatsAppEventTaskPayload.model_validate(
            {
                "event_key": EVENT_KEY,
                "phone": "5511999999999",
                "raw_payload": {"message": "sensitive"},
            }
        )

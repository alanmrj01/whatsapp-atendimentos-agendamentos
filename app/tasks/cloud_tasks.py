from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from typing import Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from app.core.config import CloudTasksConfiguration


class CloudTasksEnqueueError(RuntimeError):
    """Falha sanitizada ao publicar um evento no Cloud Tasks."""


class EventTaskEnqueuer(Protocol):
    async def enqueue(self, event_key: str) -> None: ...


class OutboundTaskEnqueuer(Protocol):
    async def enqueue(self, message_id: uuid.UUID) -> None: ...


_cloud_tasks_client: tasks_v2.CloudTasksAsyncClient | None = None


def get_cloud_tasks_client() -> tasks_v2.CloudTasksAsyncClient:
    global _cloud_tasks_client

    if _cloud_tasks_client is None:
        _cloud_tasks_client = tasks_v2.CloudTasksAsyncClient()
    return _cloud_tasks_client


class CloudTasksEventEnqueuer:
    def __init__(
        self,
        configuration: CloudTasksConfiguration,
        client: tasks_v2.CloudTasksAsyncClient | None = None,
    ) -> None:
        self.configuration = configuration
        self.client = client

    async def enqueue(self, event_key: str) -> None:
        client = _resolve_client(
            self.client,
            "WhatsApp event could not be enqueued",
        )
        await _create_task(
            client,
            self.configuration,
            task_id=deterministic_task_id(event_key),
            payload={"event_key": event_key},
            error_message="WhatsApp event could not be enqueued",
        )


class CloudTasksOutboundEnqueuer:
    def __init__(
        self,
        configuration: CloudTasksConfiguration,
        client: tasks_v2.CloudTasksAsyncClient | None = None,
    ) -> None:
        self.configuration = configuration
        self.client = client

    async def enqueue(self, message_id: uuid.UUID) -> None:
        client = _resolve_client(
            self.client,
            "WhatsApp outbound could not be enqueued",
        )
        await _create_task(
            client,
            self.configuration,
            task_id=deterministic_outbound_task_id(message_id),
            payload={"message_id": str(message_id)},
            error_message="WhatsApp outbound could not be enqueued",
        )


def deterministic_task_id(event_key: str) -> str:
    fingerprint = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    return f"whatsapp-event-{fingerprint}"


def deterministic_outbound_task_id(message_id: uuid.UUID) -> str:
    fingerprint = hashlib.sha256(str(message_id).encode("ascii")).hexdigest()
    return f"whatsapp-outbound-{fingerprint}"


def _resolve_client(
    client: tasks_v2.CloudTasksAsyncClient | None,
    error_message: str,
) -> tasks_v2.CloudTasksAsyncClient:
    if client is not None:
        return client
    try:
        return get_cloud_tasks_client()
    except Exception:
        raise CloudTasksEnqueueError(error_message) from None


async def _create_task(
    client: tasks_v2.CloudTasksAsyncClient,
    configuration: CloudTasksConfiguration,
    *,
    task_id: str,
    payload: dict[str, str],
    error_message: str,
) -> None:
    parent = (
        f"projects/{configuration.project_id}/locations/"
        f"{configuration.region}/queues/{configuration.queue}"
    )
    task = tasks_v2.Task(
        name=f"{parent}/tasks/{task_id}",
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=configuration.target_url,
            headers={"Content-Type": "application/json"},
            oidc_token=tasks_v2.OidcToken(
                service_account_email=configuration.invoker_email,
                audience=configuration.oidc_audience,
            ),
            body=json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        ),
    )
    try:
        await client.create_task(
            request=tasks_v2.CreateTaskRequest(parent=parent, task=task)
        )
    except AlreadyExists:
        return
    except Exception:
        raise CloudTasksEnqueueError(error_message) from None


async def close_cloud_tasks_client() -> None:
    global _cloud_tasks_client

    client = _cloud_tasks_client
    _cloud_tasks_client = None
    if client is None:
        return
    close_result = client.close()
    if inspect.isawaitable(close_result):
        await close_result

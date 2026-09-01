from __future__ import annotations

import hashlib
import inspect
import json
from typing import Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from app.core.config import CloudTasksConfiguration


class CloudTasksEnqueueError(RuntimeError):
    """Falha sanitizada ao publicar um evento no Cloud Tasks."""


class EventTaskEnqueuer(Protocol):
    async def enqueue(self, event_key: str) -> None: ...


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
        self.client = client or get_cloud_tasks_client()

    async def enqueue(self, event_key: str) -> None:
        parent = (
            f"projects/{self.configuration.project_id}/locations/"
            f"{self.configuration.region}/queues/{self.configuration.queue}"
        )
        task_id = deterministic_task_id(event_key)
        task = tasks_v2.Task(
            name=f"{parent}/tasks/{task_id}",
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=self.configuration.target_url,
                headers={"Content-Type": "application/json"},
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.configuration.invoker_email,
                    audience=self.configuration.oidc_audience,
                ),
                body=json.dumps(
                    {"event_key": event_key},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
            ),
        )
        try:
            await self.client.create_task(
                request=tasks_v2.CreateTaskRequest(parent=parent, task=task)
            )
        except AlreadyExists:
            return
        except Exception:
            raise CloudTasksEnqueueError(
                "WhatsApp event could not be enqueued"
            ) from None


def deterministic_task_id(event_key: str) -> str:
    fingerprint = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    return f"whatsapp-event-{fingerprint}"


async def close_cloud_tasks_client() -> None:
    global _cloud_tasks_client

    client = _cloud_tasks_client
    _cloud_tasks_client = None
    if client is None:
        return
    close_result = client.close()
    if inspect.isawaitable(close_result):
        await close_result

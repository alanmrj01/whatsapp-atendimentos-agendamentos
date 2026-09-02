from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr
from pytest import LogCaptureFixture, mark, raises

from app.core.config import Settings
from app.whatsapp.client import (
    DEFAULT_LIST_BUTTON_TEXT,
    WhatsAppAuthenticationError,
    WhatsAppClient,
    WhatsAppConfigurationError,
    WhatsAppInvalidResponseError,
    WhatsAppNetworkError,
    WhatsAppPermanentError,
    WhatsAppRateLimitError,
    WhatsAppTemporaryError,
    WhatsAppTimeoutError,
    WhatsAppValidationError,
)

EXPECTED_MESSAGES_URL = (
    "https://graph.facebook.com/v23.0/test-phone-number-id/messages"
)


def settings() -> Settings:
    return Settings(_env_file=None)


@mark.parametrize(
    ("field", "value"),
    [
        ("meta_access_token", None),
        ("meta_access_token", SecretStr("")),
        ("meta_phone_number_id", None),
        ("meta_phone_number_id", ""),
        ("meta_phone_number_id", "unsafe/id"),
        ("meta_graph_version", None),
        ("meta_graph_version", ""),
        ("meta_graph_version", "latest"),
    ],
)
def test_invalid_client_configuration_is_rejected(
    field: str,
    value: Any,
) -> None:
    invalid_settings = settings().model_copy(update={field: value})

    with raises(WhatsAppConfigurationError) as exc_info:
        WhatsAppClient(invalid_settings)

    assert str(exc_info.value) == "WhatsApp client configuration is invalid"


@mark.asyncio
async def test_send_text_success() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.text"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        provider_message_id = await client.send_text("recipient-1", "Hello")

    request = captured_requests[0]
    assert provider_message_id == "wamid.text"
    assert request.method == "POST"
    assert str(request.url) == EXPECTED_MESSAGES_URL
    assert request.headers["Authorization"] == "Bearer test-access-token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "recipient-1",
        "type": "text",
        "text": {"body": "Hello"},
    }
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }


@mark.asyncio
async def test_send_interactive_buttons_success() -> None:
    captured_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.buttons"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        provider_message_id = await client.send_interactive_buttons(
            "recipient-2",
            "Choose",
            [
                {"id": "yes", "title": "Yes"},
                {"id": "no", "title": "No"},
            ],
        )

    assert provider_message_id == "wamid.buttons"
    assert captured_payload["interactive"] == {
        "type": "button",
        "body": {"text": "Choose"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "no", "title": "No"}},
            ]
        },
    }


@mark.asyncio
async def test_send_interactive_list_success() -> None:
    captured_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.list"}]})

    sections = [
        {
            "title": "Services",
            "rows": [
                {
                    "id": "service-1",
                    "title": "Haircut",
                    "description": "Thirty minutes",
                },
                {"id": "service-2", "title": "Consultation"},
            ],
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        provider_message_id = await client.send_interactive_list(
            "recipient-3", "Choose a service", sections
        )

    assert provider_message_id == "wamid.list"
    assert captured_payload["interactive"] == {
        "type": "list",
        "body": {"text": "Choose a service"},
        "action": {
            "button": DEFAULT_LIST_BUTTON_TEXT,
            "sections": sections,
        },
    }


@mark.asyncio
async def test_mark_as_read_success() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        result = await client.mark_as_read("wamid.inbound")

    assert result is True
    assert captured_request is not None
    assert captured_request.method == "PUT"
    assert str(captured_request.url) == EXPECTED_MESSAGES_URL
    assert json.loads(captured_request.content) == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.inbound",
    }


@mark.parametrize(
    ("status_code", "expected_exception"),
    [
        (400, WhatsAppPermanentError),
        (401, WhatsAppAuthenticationError),
        (403, WhatsAppAuthenticationError),
        (429, WhatsAppRateLimitError),
        (500, WhatsAppTemporaryError),
        (503, WhatsAppTemporaryError),
    ],
)
@mark.asyncio
async def test_http_errors_are_classified_without_retry(
    status_code: int,
    expected_exception: type[Exception],
) -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            status_code,
            json={"error": {"message": "sensitive remote details"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(expected_exception):
            await client.send_text("recipient-error", "Sensitive text")

    assert request_count == 1


@mark.asyncio
async def test_timeout_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("remote timeout details", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppTimeoutError) as exc_info:
            await client.send_text("recipient-timeout", "Timeout content")

    assert exc_info.value.__cause__ is None


@mark.asyncio
async def test_network_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("remote network details", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppNetworkError) as exc_info:
            await client.send_text("recipient-network", "Network content")

    assert exc_info.value.__cause__ is None


@mark.asyncio
async def test_invalid_json_response_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppInvalidResponseError):
            await client.send_text("recipient-json", "JSON content")


@mark.asyncio
async def test_missing_provider_message_id_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppInvalidResponseError):
            await client.send_text("recipient-missing-id", "Missing ID")


InvalidOperation = Callable[[WhatsAppClient], Awaitable[Any]]


@mark.parametrize(
    "operation",
    [
        lambda client: client.send_text("", "text"),
        lambda client: client.send_text("recipient", "   "),
        lambda client: client.send_interactive_buttons("recipient", "body", []),
        lambda client: client.send_interactive_buttons(
            "recipient",
            "body",
            [
                {"id": "duplicate", "title": "One"},
                {"id": "duplicate", "title": "Two"},
            ],
        ),
        lambda client: client.send_interactive_buttons(
            "recipient", "body", [{"id": "invalid", "title": ""}]
        ),
        lambda client: client.send_interactive_list("recipient", "body", []),
        lambda client: client.send_interactive_list(
            "recipient", "body", [{"title": "Section", "rows": []}]
        ),
        lambda client: client.send_interactive_list(
            "recipient",
            "body",
            [
                {
                    "title": "Section",
                    "rows": [
                        {"id": "duplicate", "title": "One"},
                        {"id": "duplicate", "title": "Two"},
                    ],
                }
            ],
        ),
        lambda client: client.mark_as_read(""),
    ],
)
@mark.asyncio
async def test_invalid_inputs_do_not_call_meta(operation: InvalidOperation) -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"messages": [{"id": "unexpected"}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppValidationError):
            await operation(client)

    assert request_count == 0


@mark.asyncio
async def test_sensitive_values_are_absent_from_logs_and_exceptions(
    caplog: LogCaptureFixture,
) -> None:
    destination = "5511999999999"
    content = "sensitive-message-content"
    token = "test-access-token"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "sensitive-response-content"}},
        )

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        client = WhatsAppClient(settings(), http_client=http_client)
        with raises(WhatsAppPermanentError) as exc_info:
            await client.send_text(destination, content)

    combined_output = caplog.text + str(exc_info.value)
    for sensitive_value in (
        destination,
        content,
        token,
        "sensitive-response-content",
    ):
        assert sensitive_value not in combined_output

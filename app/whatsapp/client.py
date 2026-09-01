from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import SecretStr

from app.core.config import Settings

GRAPH_API_BASE_URL = "https://graph.facebook.com"
DEFAULT_LIST_BUTTON_TEXT = "Ver opções"


class WhatsAppClientError(RuntimeError):
    """Base sanitizada para falhas do cliente WhatsApp."""


class WhatsAppValidationError(WhatsAppClientError):
    """Entrada local inválida; nenhuma chamada externa foi realizada."""


class WhatsAppPermanentError(WhatsAppClientError):
    """Erro permanente retornado pela Meta."""


class WhatsAppAuthenticationError(WhatsAppPermanentError):
    """Token inválido ou sem permissão."""


class WhatsAppTemporaryError(WhatsAppClientError):
    """Falha temporária retornada pela Meta."""


class WhatsAppRateLimitError(WhatsAppTemporaryError):
    """Limite de requisições atingido."""


class WhatsAppTimeoutError(WhatsAppTemporaryError):
    """Tempo limite excedido na chamada à Meta."""


class WhatsAppNetworkError(WhatsAppTemporaryError):
    """Falha de rede durante a chamada à Meta."""


class WhatsAppInvalidResponseError(WhatsAppClientError):
    """Resposta de sucesso da Meta ausente ou malformada."""


class WhatsAppClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        access_token, phone_number_id, graph_version = (
            _validate_client_configuration(settings)
        )
        self._access_token = access_token
        self._messages_url = (
            f"{GRAPH_API_BASE_URL}/{graph_version}/"
            f"{phone_number_id}/messages"
        )
        self._timeout = timeout or httpx.Timeout(10.0, connect=5.0)
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=self._timeout,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def send_text(self, to: str, text: str) -> str:
        destination = _validate_destination(to)
        message_text = _validate_text(text, "text", max_length=4096)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destination,
            "type": "text",
            "text": {"body": message_text},
        }
        response_data = await self._request("POST", payload)
        return _provider_message_id(response_data)

    async def send_interactive_buttons(
        self,
        to: str,
        body: str,
        buttons: Sequence[Mapping[str, Any]],
    ) -> str:
        destination = _validate_destination(to)
        body_text = _validate_text(body, "body", max_length=1024)
        normalized_buttons = _validate_buttons(buttons)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destination,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": normalized_buttons},
            },
        }
        response_data = await self._request("POST", payload)
        return _provider_message_id(response_data)

    async def send_interactive_list(
        self,
        to: str,
        body: str,
        sections: Sequence[Mapping[str, Any]],
    ) -> str:
        destination = _validate_destination(to)
        body_text = _validate_text(body, "body", max_length=1024)
        normalized_sections = _validate_sections(sections)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": destination,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body_text},
                "action": {
                    "button": DEFAULT_LIST_BUTTON_TEXT,
                    "sections": normalized_sections,
                },
            },
        }
        response_data = await self._request("POST", payload)
        return _provider_message_id(response_data)

    async def mark_as_read(self, message_id: str) -> bool:
        normalized_message_id = _validate_identifier(
            message_id, "message_id", max_length=255
        )
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": normalized_message_id,
        }
        response_data = await self._request("PUT", payload)
        if response_data.get("success") is not True:
            raise WhatsAppInvalidResponseError(
                "WhatsApp API returned an invalid read receipt response"
            )
        return True

    async def _request(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await self._http_client.request(
                method,
                self._messages_url,
                headers={
                    "Authorization": (
                        "Bearer " + self._access_token.get_secret_value()
                    ),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise WhatsAppTimeoutError("WhatsApp API request timed out") from None
        except httpx.RequestError:
            raise WhatsAppNetworkError("WhatsApp API network request failed") from None

        _raise_for_status(response.status_code)
        try:
            response_data = response.json()
        except ValueError:
            raise WhatsAppInvalidResponseError(
                "WhatsApp API returned invalid JSON"
            ) from None
        if not isinstance(response_data, dict):
            raise WhatsAppInvalidResponseError(
                "WhatsApp API returned an invalid response"
            )
        return response_data


def _validate_client_configuration(
    settings: Settings,
) -> tuple[SecretStr, str, str]:
    access_token_secret = settings.meta_access_token
    phone_number_id = settings.meta_phone_number_id
    graph_version = settings.meta_graph_version
    access_token = (
        access_token_secret.get_secret_value()
        if access_token_secret is not None
        else ""
    )
    if (
        not access_token
        or access_token != access_token.strip()
        or phone_number_id is None
        or graph_version is None
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", phone_number_id)
        or not re.fullmatch(r"v\d{1,3}\.\d{1,3}", graph_version)
    ):
        raise WhatsAppValidationError(
            "WhatsApp client configuration is invalid"
        )
    return access_token_secret, phone_number_id, graph_version


def _raise_for_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise WhatsAppAuthenticationError("WhatsApp API authentication failed")
    if status_code == 429:
        raise WhatsAppRateLimitError("WhatsApp API rate limit reached")
    if 400 <= status_code < 500:
        raise WhatsAppPermanentError("WhatsApp API rejected the request")
    if 500 <= status_code < 600:
        raise WhatsAppTemporaryError("WhatsApp API is temporarily unavailable")
    raise WhatsAppInvalidResponseError("WhatsApp API returned an unexpected status")


def _provider_message_id(response_data: dict[str, Any]) -> str:
    messages = response_data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WhatsAppInvalidResponseError(
            "WhatsApp API response missing message identifier"
        )
    first_message = messages[0]
    if not isinstance(first_message, dict):
        raise WhatsAppInvalidResponseError(
            "WhatsApp API response missing message identifier"
        )
    provider_message_id = first_message.get("id")
    if not isinstance(provider_message_id, str) or not provider_message_id.strip():
        raise WhatsAppInvalidResponseError(
            "WhatsApp API response missing message identifier"
        )
    return provider_message_id


def _validate_buttons(
    buttons: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = _validate_sequence(buttons, "buttons", minimum=1, maximum=3)
    normalized_buttons: list[dict[str, Any]] = []
    button_ids: set[str] = set()
    for button in items:
        if not isinstance(button, Mapping):
            raise WhatsAppValidationError("buttons must contain objects")
        button_id = _validate_identifier(button.get("id"), "button id", 256)
        title = _validate_text(button.get("title"), "button title", 20).strip()
        if button_id in button_ids:
            raise WhatsAppValidationError("button ids must be unique")
        button_ids.add(button_id)
        normalized_buttons.append(
            {"type": "reply", "reply": {"id": button_id, "title": title}}
        )
    return normalized_buttons


def _validate_sections(
    sections: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = _validate_sequence(sections, "sections", minimum=1, maximum=10)
    normalized_sections: list[dict[str, Any]] = []
    row_ids: set[str] = set()
    total_rows = 0
    for section in items:
        if not isinstance(section, Mapping):
            raise WhatsAppValidationError("sections must contain objects")
        title = _validate_text(section.get("title"), "section title", 24).strip()
        rows = _validate_sequence(
            section.get("rows"), "section rows", minimum=1, maximum=10
        )
        normalized_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise WhatsAppValidationError("section rows must contain objects")
            row_id = _validate_identifier(row.get("id"), "row id", 200)
            row_title = _validate_text(row.get("title"), "row title", 24).strip()
            if row_id in row_ids:
                raise WhatsAppValidationError("row ids must be unique")
            row_ids.add(row_id)
            normalized_row = {"id": row_id, "title": row_title}
            if row.get("description") is not None:
                normalized_row["description"] = _validate_text(
                    row.get("description"), "row description", 72
                ).strip()
            normalized_rows.append(normalized_row)
        total_rows += len(normalized_rows)
        if total_rows > 10:
            raise WhatsAppValidationError("lists support at most 10 rows")
        normalized_sections.append({"title": title, "rows": normalized_rows})
    return normalized_sections


def _validate_destination(value: Any) -> str:
    destination = _validate_identifier(value, "destination", max_length=255)
    if any(character.isspace() for character in destination):
        raise WhatsAppValidationError("destination must not contain whitespace")
    return destination


def _validate_identifier(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise WhatsAppValidationError(f"{field} must be a string")
    if value != value.strip() or not value:
        raise WhatsAppValidationError(f"{field} must not be empty or padded")
    if len(value) > max_length:
        raise WhatsAppValidationError(f"{field} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise WhatsAppValidationError(f"{field} contains invalid characters")
    return value


def _validate_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise WhatsAppValidationError(f"{field} must be a string")
    if not value.strip():
        raise WhatsAppValidationError(f"{field} must not be empty")
    if len(value) > max_length:
        raise WhatsAppValidationError(f"{field} is too long")
    return value


def _validate_sequence(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise WhatsAppValidationError(f"{field} must be a list")
    if not minimum <= len(value) <= maximum:
        raise WhatsAppValidationError(
            f"{field} must contain between {minimum} and {maximum} items"
        )
    return value

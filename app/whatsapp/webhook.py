from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeAlias

from app.schemas.whatsapp_webhook import WhatsAppWebhookPayload

SUPPORTED_MESSAGE_STATUSES = {"sent", "delivered", "read", "failed"}
INDIVIDUAL_WHATSAPP_ID_PATTERN = re.compile(r"^[1-9][0-9]{6,14}$")
COLLECTIVE_IDENTIFIER_SUFFIXES = (
    "@g.us",
    "@broadcast",
    "@newsletter",
    "@community",
    "@channel",
)
COLLECTIVE_INDICATOR_KEYS = {
    "broadcast_id",
    "channel_id",
    "community_id",
    "group_id",
    "newsletter_id",
}
CONVERSATION_KIND_KEYS = {
    "chat_type",
    "conversation_type",
    "recipient_type",
    "source_type",
}
INDIVIDUAL_CONVERSATION_KINDS = {
    "1:1",
    "individual",
    "one_to_one",
    "personal",
    "person",
    "user",
}


@dataclass(frozen=True, slots=True)
class InboundMessageEvent:
    event_key: str
    event_type: str
    meta_phone_number_id: str
    provider_message_id: str
    whatsapp_id: str
    message_type: str
    body: str | None
    interactive_id: str | None


@dataclass(frozen=True, slots=True)
class MessageStatusEvent:
    event_key: str
    event_type: str
    meta_phone_number_id: str
    provider_message_id: str
    message_status: str


@dataclass(frozen=True, slots=True)
class BusinessMessageEchoEvent:
    event_key: str
    event_type: str
    meta_phone_number_id: str
    provider_message_id: str
    whatsapp_id: str
    message_type: str
    occurred_at: datetime


NormalizedWebhookEvent: TypeAlias = (
    InboundMessageEvent | MessageStatusEvent | BusinessMessageEchoEvent
)


def verify_meta_signature(
    raw_body: bytes,
    signature: str | None,
    app_secret: str,
) -> bool:
    expected_signature = (
        "sha256="
        + hmac.new(
            app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
    ).encode("ascii")
    try:
        provided_signature = (signature or "").encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(provided_signature, expected_signature)


def build_event_key(event_kind: str, *parts: str) -> str:
    fingerprint = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"whatsapp:{event_kind}:{fingerprint}"


def normalize_webhook_payload(
    payload: WhatsAppWebhookPayload,
) -> list[NormalizedWebhookEvent]:
    if payload.object != "whatsapp_business_account":
        return []

    events: list[NormalizedWebhookEvent] = []
    for entry in payload.entry:
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            meta_phone_number_id = _meta_phone_number_id(value)
            if meta_phone_number_id is None:
                continue
            field = change.get("field")
            if field == "messages":
                events.extend(_normalize_messages(value, meta_phone_number_id))
                events.extend(_normalize_statuses(value, meta_phone_number_id))
            elif field == "smb_message_echoes":
                events.extend(
                    _normalize_business_message_echoes(
                        value, meta_phone_number_id
                    )
                )
    return events


def _normalize_messages(
    value: dict[str, Any], meta_phone_number_id: str
) -> list[InboundMessageEvent]:
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        return []

    events: list[InboundMessageEvent] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        provider_message_id = _identifier(raw_message.get("id"), 255)
        whatsapp_id = _identifier(raw_message.get("from"), 255)
        message_type = _identifier(raw_message.get("type"), 40)
        if not provider_message_id or not whatsapp_id or not message_type:
            continue
        if not _is_individual_message(value, raw_message, whatsapp_id):
            continue

        body, interactive_id = _message_content(raw_message, message_type)
        events.append(
            InboundMessageEvent(
                event_key=build_event_key("inbound", provider_message_id),
                event_type=f"message.inbound.{message_type}",
                meta_phone_number_id=meta_phone_number_id,
                provider_message_id=provider_message_id,
                whatsapp_id=whatsapp_id,
                message_type=message_type,
                body=body,
                interactive_id=interactive_id,
            )
        )
    return events


def _normalize_statuses(
    value: dict[str, Any], meta_phone_number_id: str
) -> list[MessageStatusEvent]:
    raw_statuses = value.get("statuses")
    if not isinstance(raw_statuses, list):
        return []

    events: list[MessageStatusEvent] = []
    for raw_status in raw_statuses:
        if not isinstance(raw_status, dict):
            continue
        provider_message_id = _identifier(raw_status.get("id"), 255)
        message_status = _identifier(raw_status.get("status"), 32)
        if (
            not provider_message_id
            or message_status not in SUPPORTED_MESSAGE_STATUSES
        ):
            continue
        events.append(
            MessageStatusEvent(
                event_key=build_event_key(
                    "status", provider_message_id, message_status
                ),
                event_type=f"message.status.{message_status}",
                meta_phone_number_id=meta_phone_number_id,
                provider_message_id=provider_message_id,
                message_status=message_status,
            )
        )
    return events


def _normalize_business_message_echoes(
    value: dict[str, Any], meta_phone_number_id: str
) -> list[BusinessMessageEchoEvent]:
    raw_echoes = value.get("message_echoes")
    if not isinstance(raw_echoes, list):
        return []

    events: list[BusinessMessageEchoEvent] = []
    for raw_echo in raw_echoes:
        if not isinstance(raw_echo, dict):
            continue
        provider_message_id = _identifier(raw_echo.get("id"), 255)
        whatsapp_id = _identifier(raw_echo.get("to"), 255)
        message_type = _identifier(raw_echo.get("type"), 40)
        occurred_at = _unix_timestamp(raw_echo.get("timestamp"))
        if (
            not provider_message_id
            or not whatsapp_id
            or not message_type
            or occurred_at is None
            or not _is_individual_message(value, raw_echo, whatsapp_id)
        ):
            continue
        events.append(
            BusinessMessageEchoEvent(
                event_key=build_event_key(
                    "business_echo", provider_message_id
                ),
                event_type=f"message.business_echo.{message_type}",
                meta_phone_number_id=meta_phone_number_id,
                provider_message_id=provider_message_id,
                whatsapp_id=whatsapp_id,
                message_type=message_type,
                occurred_at=occurred_at,
            )
        )
    return events


def _meta_phone_number_id(value: dict[str, Any]) -> str | None:
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _identifier(metadata.get("phone_number_id"), 255)


def is_individual_whatsapp_id(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized.endswith(COLLECTIVE_IDENTIFIER_SUFFIXES):
        return False
    return INDIVIDUAL_WHATSAPP_ID_PATTERN.fullmatch(normalized) is not None


def _is_individual_message(
    value: dict[str, Any],
    raw_message: dict[str, Any],
    whatsapp_id: str,
) -> bool:
    if not is_individual_whatsapp_id(whatsapp_id):
        return False
    return not (
        _has_collective_indicator(value) or _has_collective_indicator(raw_message)
    )


def _has_collective_indicator(value: dict[str, Any]) -> bool:
    for key, raw_indicator in value.items():
        normalized_key = key.casefold()
        if normalized_key in COLLECTIVE_INDICATOR_KEYS and raw_indicator not in (
            None,
            "",
            False,
        ):
            return True
        if normalized_key in CONVERSATION_KIND_KEYS:
            if not isinstance(raw_indicator, str):
                return True
            if raw_indicator.strip().casefold() not in INDIVIDUAL_CONVERSATION_KINDS:
                return True
        if normalized_key in {"context", "metadata"} and isinstance(
            raw_indicator, dict
        ):
            if _has_collective_indicator(raw_indicator):
                return True
    return False


def _message_content(
    raw_message: dict[str, Any], message_type: str
) -> tuple[str | None, str | None]:
    if message_type == "text":
        text_content = raw_message.get("text")
        if isinstance(text_content, dict):
            return _body(text_content.get("body")), None
        return None, None

    if message_type == "interactive":
        interactive = raw_message.get("interactive")
        if not isinstance(interactive, dict):
            return None, None
        reply_type = _identifier(interactive.get("type"), 64)
        reply = interactive.get(reply_type) if reply_type else None
        if not isinstance(reply, dict):
            return None, None
        return _body(reply.get("title")), _identifier(reply.get("id"), 255)

    media_content = raw_message.get(message_type)
    if isinstance(media_content, dict):
        return _body(media_content.get("caption")), None
    return None, None


def _identifier(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _body(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _unix_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

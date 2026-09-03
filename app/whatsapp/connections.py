from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class WhatsAppConnectionMode(str, Enum):
    COEXISTENCE = "coexistence"
    API_ONLY = "api_only"


class WhatsAppConnectionStatus(str, Enum):
    PENDING = "pending"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class WhatsAppProvider(str, Enum):
    META = "meta"


@dataclass(frozen=True, slots=True)
class WhatsAppConnectionRecord:
    id: uuid.UUID
    business_id: uuid.UUID
    provider: WhatsAppProvider
    mode: WhatsAppConnectionMode
    status: WhatsAppConnectionStatus
    meta_waba_id: str | None
    meta_phone_number_id: str | None
    credential_secret_ref: str | None
    graph_version: str | None
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    last_error_code: str | None = None


_META_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}")
_GRAPH_VERSION_PATTERN = re.compile(r"v\d{1,3}\.\d{1,3}")
_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SECRET_REFERENCE_PATTERN = re.compile(
    r"projects/[A-Za-z0-9._-]{1,128}/secrets/"
    r"[A-Za-z0-9_-]{1,255}/versions/(?:[A-Za-z0-9_-]{1,64})"
)


def validate_meta_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if not _META_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError("Meta identifier is invalid")
    return value


def validate_graph_version(value: str | None) -> str | None:
    if value is None:
        return None
    if not _GRAPH_VERSION_PATTERN.fullmatch(value):
        raise ValueError("Graph version is invalid")
    return value


def validate_credential_secret_ref(value: str) -> str:
    if not _SECRET_REFERENCE_PATTERN.fullmatch(value):
        raise ValueError("Credential secret reference is invalid")
    return value


def sanitize_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    if _ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return "provider_error"

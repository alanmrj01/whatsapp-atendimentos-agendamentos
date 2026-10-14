from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.core.config import MetaEmbeddedSignupConfiguration
from app.whatsapp.embedded_signup import MetaEmbeddedSignupRejected


_STATE_CONTEXT = b"alovia-meta-embedded-signup-redirect-v2\0"
_MAX_STATE_LENGTH = 2048
_CALLBACK_PATH = "/api/v1/whatsapp/onboarding/embedded-signup/redirect/callback"
_RETURN_PATH = "/app/whatsapp/business"
_RETURN_RESULTS = frozenset({"success", "cancelled", "error"})


@dataclass(frozen=True, slots=True)
class MetaEmbeddedSignupRedirectState:
    business_id: uuid.UUID
    attempt_id: uuid.UUID
    callback_uri: str
    return_uri: str
    return_to_onboarding: bool
    expires_at: datetime


def validate_meta_embedded_signup_return_uri(
    value: str,
    *,
    allowed_origins: tuple[str, ...],
) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        raise MetaEmbeddedSignupRejected("Meta return URI is invalid")
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != _RETURN_PATH
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise MetaEmbeddedSignupRejected("Meta return URI is invalid") from None

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in allowed_origins:
        raise MetaEmbeddedSignupRejected("Meta return URI is invalid")
    return candidate


def validate_meta_embedded_signup_callback_uri(value: str | None) -> str:
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate or len(candidate) > 2048:
        raise MetaEmbeddedSignupRejected("Meta callback URI is invalid")
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != _CALLBACK_PATH
            or (parsed.port is not None and parsed.port != 443)
        ):
            raise ValueError
    except ValueError:
        raise MetaEmbeddedSignupRejected("Meta callback URI is invalid") from None
    return candidate


def build_meta_embedded_signup_redirect_state(
    configuration: MetaEmbeddedSignupConfiguration,
    *,
    business_id: uuid.UUID,
    attempt_id: uuid.UUID,
    callback_uri: str,
    return_uri: str,
    return_to_onboarding: bool,
    expires_at: datetime,
) -> str:
    expiry = expires_at.astimezone(UTC)
    payload = {
        "v": 2,
        "business_id": str(business_id),
        "attempt_id": str(attempt_id),
        "callback_uri": callback_uri,
        "return_uri": return_uri,
        "return_to_onboarding": return_to_onboarding,
        "exp": int(expiry.timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded_payload = _b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        configuration.app_secret.get_secret_value().encode("utf-8"),
        _STATE_CONTEXT + encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def parse_meta_embedded_signup_redirect_state(
    configuration: MetaEmbeddedSignupConfiguration,
    state: str,
    *,
    now: datetime | None = None,
) -> MetaEmbeddedSignupRedirectState:
    raw = state.strip()
    if not raw or len(raw) > _MAX_STATE_LENGTH:
        raise MetaEmbeddedSignupRejected("Meta authorization state is invalid")
    try:
        encoded_payload, encoded_signature = raw.split(".", 1)
        expected = hmac.new(
            configuration.app_secret.get_secret_value().encode("utf-8"),
            _STATE_CONTEXT + encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        actual = _b64decode(encoded_signature)
        if not hmac.compare_digest(expected, actual):
            raise ValueError

        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 2:
            raise ValueError
        business_id = uuid.UUID(str(payload["business_id"]))
        attempt_id = uuid.UUID(str(payload["attempt_id"]))
        callback_uri = payload["callback_uri"]
        return_uri = payload["return_uri"]
        return_to_onboarding = payload["return_to_onboarding"]
        expiry_value = payload["exp"]
        nonce = payload["nonce"]
        if (
            not isinstance(callback_uri, str)
            or not isinstance(return_uri, str)
            or not isinstance(return_to_onboarding, bool)
            or not isinstance(expiry_value, int)
            or not isinstance(nonce, str)
            or not nonce
        ):
            raise ValueError
        expires_at = datetime.fromtimestamp(expiry_value, tz=UTC)
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        raise MetaEmbeddedSignupRejected(
            "Meta authorization state is invalid"
        ) from None

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if expires_at <= current:
        raise MetaEmbeddedSignupRejected("Meta authorization state is expired")

    return MetaEmbeddedSignupRedirectState(
        business_id=business_id,
        attempt_id=attempt_id,
        callback_uri=callback_uri,
        return_uri=return_uri,
        return_to_onboarding=return_to_onboarding,
        expires_at=expires_at,
    )


def build_meta_embedded_signup_authorization_url(
    configuration: MetaEmbeddedSignupConfiguration,
    *,
    state: str,
    callback_uri: str,
) -> str:
    params = {
        "client_id": configuration.app_id,
        "config_id": configuration.configuration_id,
        "redirect_uri": callback_uri,
        "response_type": "code",
        "override_default_response_type": "true",
        "auth_type": "rerequest",
        "state": state,
        "extras": json.dumps(
            {
                "setup": {},
                "featureType": "whatsapp_business_app_onboarding",
            },
            separators=(",", ":"),
        ),
    }
    return (
        f"https://www.facebook.com/{configuration.graph_version}/dialog/oauth?"
        f"{urlencode(params)}"
    )


def build_meta_embedded_signup_return_url(
    state: MetaEmbeddedSignupRedirectState,
    *,
    result: str,
) -> str:
    if result not in _RETURN_RESULTS:
        raise ValueError("Unsupported Meta return result")
    parsed = urlsplit(state.return_uri)
    query = {"meta_return": result}
    if state.return_to_onboarding:
        query["from"] = "onboarding"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            "",
        )
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value:
        raise ValueError
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)

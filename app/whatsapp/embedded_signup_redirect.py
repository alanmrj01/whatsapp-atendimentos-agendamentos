from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

from app.core.config import MetaEmbeddedSignupConfiguration


META_REDIRECT_STATE_TTL = timedelta(minutes=15)


class MetaEmbeddedSignupRedirectError(ValueError):
    """Invalid or expired OAuth redirect state."""


@dataclass(frozen=True, slots=True)
class MetaEmbeddedSignupRedirectState:
    business_id: UUID
    attempt_id: UUID
    redirect_uri: str
    return_uri: str


def build_meta_embedded_signup_redirect(
    configuration: MetaEmbeddedSignupConfiguration,
    *,
    business_id: UUID,
    attempt_id: UUID,
    redirect_uri: str,
    return_uri: str,
    now: datetime | None = None,
) -> tuple[str, str]:
    state = _encode_state(
        configuration,
        business_id=business_id,
        attempt_id=attempt_id,
        redirect_uri=redirect_uri,
        return_uri=return_uri,
        now=now,
    )
    params = {
        "client_id": configuration.app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "config_id": configuration.configuration_id,
        "auth_type": "rerequest",
        "override_default_response_type": "true",
        "state": state,
        "display": "page",
        "extras": json.dumps(
            {
                "setup": {},
                "featureType": "whatsapp_business_app_onboarding",
            },
            separators=(",", ":"),
        ),
    }
    authorization_url = (
        f"https://www.facebook.com/{configuration.graph_version}/dialog/oauth?"
        f"{urlencode(params)}"
    )
    return authorization_url, state


def decode_meta_embedded_signup_redirect_state(
    configuration: MetaEmbeddedSignupConfiguration,
    raw_state: str,
    *,
    now: datetime | None = None,
) -> MetaEmbeddedSignupRedirectState:
    if not raw_state or len(raw_state) > 4096 or raw_state.count(".") != 1:
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid")
    encoded_payload, encoded_signature = raw_state.split(".", 1)
    expected = _sign(configuration, encoded_payload)
    try:
        supplied = _b64decode(encoded_signature)
    except ValueError:
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid") from None
    if not hmac.compare_digest(expected, supplied):
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid")
    try:
        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid")
    current = now or datetime.now(UTC)
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= int(current.timestamp()):
        raise MetaEmbeddedSignupRedirectError("Meta redirect state expired")
    try:
        business_id = UUID(str(payload["business_id"]))
        attempt_id = UUID(str(payload["attempt_id"]))
    except (KeyError, TypeError, ValueError):
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid") from None
    redirect_uri = payload.get("redirect_uri")
    return_uri = payload.get("return_uri")
    if (
        not isinstance(redirect_uri, str)
        or not (1 <= len(redirect_uri) <= 2048)
        or not isinstance(return_uri, str)
        or not (1 <= len(return_uri) <= 2048)
    ):
        raise MetaEmbeddedSignupRedirectError("Meta redirect state is invalid")
    return MetaEmbeddedSignupRedirectState(
        business_id=business_id,
        attempt_id=attempt_id,
        redirect_uri=redirect_uri,
        return_uri=return_uri,
    )


def _encode_state(
    configuration: MetaEmbeddedSignupConfiguration,
    *,
    business_id: UUID,
    attempt_id: UUID,
    redirect_uri: str,
    return_uri: str,
    now: datetime | None,
) -> str:
    current = now or datetime.now(UTC)
    payload = {
        "v": 1,
        "business_id": str(business_id),
        "attempt_id": str(attempt_id),
        "redirect_uri": redirect_uri,
        "return_uri": return_uri,
        "exp": int((current + META_REDIRECT_STATE_TTL).timestamp()),
        "nonce": secrets.token_urlsafe(12),
    }
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64encode(_sign(configuration, encoded_payload))
    return f"{encoded_payload}.{signature}"


def _sign(
    configuration: MetaEmbeddedSignupConfiguration,
    encoded_payload: str,
) -> bytes:
    return hmac.new(
        configuration.app_secret.get_secret_value().encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError):
        raise ValueError("invalid base64") from None

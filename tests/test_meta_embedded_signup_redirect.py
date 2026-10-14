from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from app.core.config import MetaEmbeddedSignupConfiguration
from app.whatsapp.embedded_signup_redirect import (
    META_REDIRECT_STATE_TTL,
    MetaEmbeddedSignupRedirectError,
    build_meta_embedded_signup_redirect,
    decode_meta_embedded_signup_redirect_state,
)


BUSINESS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ATTEMPT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
REDIRECT_URI = (
    "https://alovia.netlify.app/api/v1/whatsapp/onboarding/"
    "embedded-signup/redirect/callback"
)
RETURN_URI = "https://alovia.netlify.app/app/whatsapp/business?from=onboarding"


def configuration() -> MetaEmbeddedSignupConfiguration:
    return MetaEmbeddedSignupConfiguration(
        app_id="333333333333333",
        configuration_id="444444444444444",
        graph_version="v25.0",
        embedded_signup_version="v4",
        app_secret=SecretStr("synthetic-app-secret-for-tests"),
        gcp_project_id="test-project",
    )


def test_redirect_url_is_full_page_oauth_and_preserves_coexistence() -> None:
    now = datetime(2026, 10, 13, 23, 45, tzinfo=UTC)
    authorization_url, state = build_meta_embedded_signup_redirect(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        redirect_uri=REDIRECT_URI,
        return_uri=RETURN_URI,
        now=now,
    )

    parsed = urlsplit(authorization_url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.facebook.com"
    assert parsed.path == "/v25.0/dialog/oauth"
    assert params["client_id"] == ["333333333333333"]
    assert params["config_id"] == ["444444444444444"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["response_type"] == ["code"]
    assert params["auth_type"] == ["rerequest"]
    assert params["display"] == ["page"]
    assert json.loads(params["extras"][0]) == {
        "setup": {},
        "featureType": "whatsapp_business_app_onboarding",
    }
    decoded = decode_meta_embedded_signup_redirect_state(
        configuration(),
        state,
        now=now,
    )
    assert decoded.business_id == BUSINESS_ID
    assert decoded.attempt_id == ATTEMPT_ID
    assert decoded.redirect_uri == REDIRECT_URI
    assert decoded.return_uri == RETURN_URI


def test_redirect_state_rejects_tampering_and_expiry() -> None:
    now = datetime(2026, 10, 13, 23, 45, tzinfo=UTC)
    _, state = build_meta_embedded_signup_redirect(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        redirect_uri=REDIRECT_URI,
        return_uri=RETURN_URI,
        now=now,
    )
    payload, signature = state.split(".", 1)
    tampered = f"{payload[:-1]}A.{signature}"
    with pytest.raises(MetaEmbeddedSignupRedirectError):
        decode_meta_embedded_signup_redirect_state(
            configuration(),
            tampered,
            now=now,
        )

    with pytest.raises(
        MetaEmbeddedSignupRedirectError,
        match="expired",
    ):
        decode_meta_embedded_signup_redirect_state(
            configuration(),
            state,
            now=now + META_REDIRECT_STATE_TTL + timedelta(seconds=1),
        )

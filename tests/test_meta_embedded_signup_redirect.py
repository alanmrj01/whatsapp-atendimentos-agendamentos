from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.auth.schemas import MetaEmbeddedSignupRedirectStartRequest
from app.core.config import MetaEmbeddedSignupConfiguration, Settings, get_settings
from app.main import app
from app.whatsapp.embedded_signup import (
    MetaEmbeddedSignupGateway,
    MetaEmbeddedSignupRejected,
)
from app.whatsapp.embedded_signup_redirect import (
    build_meta_embedded_signup_authorization_url,
    build_meta_embedded_signup_redirect_state,
    build_meta_embedded_signup_return_url,
    parse_meta_embedded_signup_redirect_state,
    validate_meta_embedded_signup_callback_uri,
    validate_meta_embedded_signup_return_uri,
)


BUSINESS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ATTEMPT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
WABA_ID = "111111111111111"
PHONE_ID = "222222222222222"
RAW_CODE = "synthetic-redirect-code"
RAW_TOKEN = "synthetic-redirect-token"
CALLBACK_URI = (
    "https://whatsapp-backend.example.test/api/v1/whatsapp/onboarding/"
    "embedded-signup/redirect/callback"
)
RETURN_URI = "https://alovia.netlify.app/app/whatsapp/business"


def configuration() -> MetaEmbeddedSignupConfiguration:
    return MetaEmbeddedSignupConfiguration(
        app_id="333333333333333",
        configuration_id="444444444444444",
        graph_version="v25.0",
        embedded_signup_version="v4",
        app_secret=SecretStr("synthetic-app-secret-for-redirect-tests"),
        gcp_project_id="test-project",
        redirect_uri=CALLBACK_URI,
    )


def test_redirect_state_and_authorization_url_are_bound_to_server_callback() -> None:
    now = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    callback_uri = validate_meta_embedded_signup_callback_uri(CALLBACK_URI)
    return_uri = validate_meta_embedded_signup_return_uri(
        RETURN_URI,
        allowed_origins=("https://alovia.netlify.app",),
    )
    state = build_meta_embedded_signup_redirect_state(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        callback_uri=callback_uri,
        return_uri=return_uri,
        return_to_onboarding=True,
        expires_at=now + timedelta(minutes=15),
    )
    parsed = parse_meta_embedded_signup_redirect_state(
        configuration(),
        state,
        now=now,
    )

    assert parsed.business_id == BUSINESS_ID
    assert parsed.attempt_id == ATTEMPT_ID
    assert parsed.callback_uri == CALLBACK_URI
    assert parsed.return_uri == RETURN_URI
    assert parsed.return_to_onboarding is True
    assert parsed.expires_at == now + timedelta(minutes=15)

    authorization_url = build_meta_embedded_signup_authorization_url(
        configuration(),
        state=state,
        callback_uri=CALLBACK_URI,
    )
    url = urlsplit(authorization_url)
    query = parse_qs(url.query)

    assert url.scheme == "https"
    assert url.netloc == "www.facebook.com"
    assert url.path == "/v25.0/dialog/oauth"
    assert query["client_id"] == ["333333333333333"]
    assert query["config_id"] == ["444444444444444"]
    assert query["redirect_uri"] == [CALLBACK_URI]
    assert query["response_type"] == ["code"]
    assert query["override_default_response_type"] == ["true"]
    assert query["auth_type"] == ["rerequest"]
    assert query["state"] == [state]
    assert json.loads(query["extras"][0]) == {
        "setup": {},
        "featureType": "whatsapp_business_app_onboarding",
    }

    clean_return = build_meta_embedded_signup_return_url(
        parsed,
        result="success",
    )
    return_url = urlsplit(clean_return)
    assert return_url.scheme == "https"
    assert return_url.netloc == "alovia.netlify.app"
    assert return_url.path == "/app/whatsapp/business"
    assert parse_qs(return_url.query) == {
        "meta_return": ["success"],
        "from": ["onboarding"],
    }
    assert RAW_CODE not in clean_return
    assert state not in clean_return


def test_redirect_state_rejects_tampering_expiry_and_untrusted_return_urls() -> None:
    now = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    state = build_meta_embedded_signup_redirect_state(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        callback_uri=CALLBACK_URI,
        return_uri=RETURN_URI,
        return_to_onboarding=False,
        expires_at=now + timedelta(minutes=1),
    )

    with pytest.raises(MetaEmbeddedSignupRejected):
        parse_meta_embedded_signup_redirect_state(
            configuration(),
            state[:-1] + ("A" if state[-1] != "A" else "B"),
            now=now,
        )

    with pytest.raises(
        MetaEmbeddedSignupRejected,
        match="state is expired",
    ):
        parse_meta_embedded_signup_redirect_state(
            configuration(),
            state,
            now=now + timedelta(minutes=2),
        )

    for invalid in (
        "https://evil.example/app/whatsapp/business",
        "http://alovia.netlify.app/app/whatsapp/business",
        "https://alovia.netlify.app/app/whatsapp/business?code=leak",
        "https://alovia.netlify.app/app/whatsapp",
    ):
        with pytest.raises(MetaEmbeddedSignupRejected):
            validate_meta_embedded_signup_return_uri(
                invalid,
                allowed_origins=("https://alovia.netlify.app",),
            )


def test_callback_uri_is_explicit_https_and_cannot_be_client_supplied() -> None:
    assert validate_meta_embedded_signup_callback_uri(CALLBACK_URI) == CALLBACK_URI

    for invalid in (
        None,
        "",
        "http://whatsapp-backend.example.test/api/v1/whatsapp/onboarding/embedded-signup/redirect/callback",
        "https://evil.example/callback",
        CALLBACK_URI + "?next=https://evil.example",
    ):
        with pytest.raises(MetaEmbeddedSignupRejected):
            validate_meta_embedded_signup_callback_uri(invalid)

    request = MetaEmbeddedSignupRedirectStartRequest(
        attempt_id=ATTEMPT_ID,
        return_uri=RETURN_URI,
        return_to_onboarding=True,
    )
    assert request.return_uri == RETURN_URI
    with pytest.raises(ValidationError):
        MetaEmbeddedSignupRedirectStartRequest.model_validate(
            {
                "attempt_id": str(ATTEMPT_ID),
                "return_uri": RETURN_URI,
                "callback_uri": "https://evil.example/callback",
            }
        )


@pytest.mark.asyncio
async def test_code_exchange_uses_exact_server_callback_uri() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            assert request.url.params["redirect_uri"] == CALLBACK_URI
            return httpx.Response(200, json={"access_token": RAW_TOKEN})
        if request.url.path.endswith(f"/{WABA_ID}"):
            return httpx.Response(200, json={"id": WABA_ID})
        if request.url.path.endswith(f"/{WABA_ID}/phone_numbers"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": PHONE_ID,
                            "display_phone_number": "+55 12 99999-1234",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected Graph request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        assets = await MetaEmbeddedSignupGateway(
            configuration(),
            client=client,
        ).exchange_and_validate(
            SecretStr(RAW_CODE),
            waba_id_hint=WABA_ID,
            phone_number_id_hint=PHONE_ID,
            redirect_uri=CALLBACK_URI,
        )

    assert assets.waba_id == WABA_ID
    assert assets.phone_number_id == PHONE_ID
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_public_callback_requires_valid_signed_state_not_user_auth(
    client: httpx.AsyncClient,
) -> None:
    test_settings = Settings(
        _env_file=None,
        ENVIRONMENT="test",
        AUTH_JWT_SECRET="x" * 48,
        PWA_ALLOWED_ORIGINS="https://alovia.netlify.app",
        META_APP_ID="333333333333333",
        META_EMBEDDED_SIGNUP_CONFIG_ID="444444444444444",
        META_EMBEDDED_SIGNUP_VERSION="v4",
        META_EMBEDDED_SIGNUP_REDIRECT_URI=CALLBACK_URI,
        META_GRAPH_VERSION="v25.0",
        META_APP_SECRET="synthetic-app-secret-for-redirect-tests",
        GCP_PROJECT_ID="test-project",
    )
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        missing = await client.get(
            "/api/v1/whatsapp/onboarding/embedded-signup/redirect/callback"
        )
        tampered = await client.get(
            "/api/v1/whatsapp/onboarding/embedded-signup/redirect/callback",
            params={"code": RAW_CODE, "state": "tampered-state"},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert missing.status_code == 400
    assert missing.json()["detail"] == "Meta authorization state is missing"
    assert tampered.status_code == 400
    assert tampered.json()["detail"] == (
        "Meta authorization state could not be validated"
    )
    assert missing.status_code != 401
    assert tampered.status_code != 401

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.auth.schemas import MetaEmbeddedSignupCompleteRequest
from app.core.config import MetaEmbeddedSignupConfiguration
from app.whatsapp.embedded_signup import (
    MetaEmbeddedSignupGateway,
    MetaEmbeddedSignupRejected,
)
from app.whatsapp.embedded_signup_redirect import (
    build_meta_embedded_signup_authorization_url,
    build_meta_embedded_signup_redirect_state,
    parse_meta_embedded_signup_redirect_state,
    validate_meta_embedded_signup_redirect_uri,
)


BUSINESS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ATTEMPT_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
WABA_ID = "111111111111111"
PHONE_ID = "222222222222222"
RAW_CODE = "synthetic-redirect-code"
RAW_TOKEN = "synthetic-redirect-token"
REDIRECT_URI = "https://alovia.netlify.app/app/whatsapp/business"


def configuration() -> MetaEmbeddedSignupConfiguration:
    return MetaEmbeddedSignupConfiguration(
        app_id="333333333333333",
        configuration_id="444444444444444",
        graph_version="v25.0",
        embedded_signup_version="v4",
        app_secret=SecretStr("synthetic-app-secret-for-redirect-tests"),
        gcp_project_id="test-project",
    )


def test_redirect_state_round_trip_and_authorization_url_are_bound_to_attempt() -> None:
    now = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
    redirect_uri = validate_meta_embedded_signup_redirect_uri(
        REDIRECT_URI,
        allowed_origins=("https://alovia.netlify.app",),
    )
    state = build_meta_embedded_signup_redirect_state(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        redirect_uri=redirect_uri,
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
    assert parsed.redirect_uri == REDIRECT_URI
    assert parsed.return_to_onboarding is True
    assert parsed.expires_at == now + timedelta(minutes=15)

    authorization_url = build_meta_embedded_signup_authorization_url(
        configuration(),
        state=state,
        redirect_uri=REDIRECT_URI,
    )
    url = urlsplit(authorization_url)
    query = parse_qs(url.query)

    assert url.scheme == "https"
    assert url.netloc == "www.facebook.com"
    assert url.path == "/v25.0/dialog/oauth"
    assert query["client_id"] == ["333333333333333"]
    assert query["config_id"] == ["444444444444444"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["override_default_response_type"] == ["true"]
    assert query["auth_type"] == ["rerequest"]
    assert query["state"] == [state]
    assert json.loads(query["extras"][0]) == {
        "setup": {},
        "featureType": "whatsapp_business_app_onboarding",
    }


def test_redirect_state_rejects_tampering_expiry_and_untrusted_return_urls() -> None:
    now = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
    state = build_meta_embedded_signup_redirect_state(
        configuration(),
        business_id=BUSINESS_ID,
        attempt_id=ATTEMPT_ID,
        redirect_uri=REDIRECT_URI,
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
            validate_meta_embedded_signup_redirect_uri(
                invalid,
                allowed_origins=("https://alovia.netlify.app",),
            )


def test_completion_contract_accepts_signed_state_but_never_client_redirect_uri() -> None:
    payload = MetaEmbeddedSignupCompleteRequest(
        authorization_code=SecretStr(RAW_CODE),
        state="signed-state",
    )
    assert payload.state == "signed-state"
    assert payload.attempt_id is None

    with pytest.raises(ValidationError):
        MetaEmbeddedSignupCompleteRequest.model_validate(
            {
                "authorization_code": RAW_CODE,
                "state": "signed-state",
                "redirect_uri": REDIRECT_URI,
            }
        )


@pytest.mark.asyncio
async def test_code_exchange_uses_exact_redirect_uri_from_manual_oauth_flow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            assert request.url.params["redirect_uri"] == REDIRECT_URI
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
            redirect_uri=REDIRECT_URI,
        )

    assert assets.waba_id == WABA_ID
    assert assets.phone_number_id == PHONE_ID
    assert len(requests) == 3

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.auth.schemas import (
    MembershipResponse,
    MembershipRole,
)
from app.core.config import get_settings
from app.main import create_app
from app.mcp_server.oauth_authorization import (
    AuthenticatedAuthorization,
    AuthorizationCompletion,
    AuthorizationDenial,
    PendingAuthorization,
)
from app.mcp_server.oauth_http import (
    OAuthHTTPServices,
    build_oauth_router,
)
from app.mcp_server.oauth_lifecycle import (
    OAuthTokenSet,
)
from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    CHATGPT_REDIRECT_URI,
)
from app.mcp_server.runtime import (
    MCPConfigurationError,
)


ISSUER = "https://backend.example.com"
RESOURCE = "https://backend.example.com/mcp-v2"
SIGNING_KEY = "s" * 64

VERIFIER = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-._~"
)

CHALLENGE = (
    base64.urlsafe_b64encode(
        hashlib.sha256(
            VERIFIER.encode("ascii")
        ).digest()
    )
    .rstrip(b"=")
    .decode("ascii")
)


class FakeMetadataVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self) -> None:
        self.calls += 1


class FakeCoordinator:
    def __init__(self) -> None:
        self.business_id = uuid4()
        self.begin_calls = 0
        self.authenticate_calls = 0
        self.authorize_calls = 0
        self.deny_calls = 0

    async def begin(self, request):
        self.begin_calls += 1

        return PendingAuthorization(
            flow_token="flow-token",
            expires_at=(
                datetime.now(UTC)
                + timedelta(minutes=10)
            ),
        )

    async def authenticate(
        self,
        *,
        flow_token: str,
        email: str,
        password: str,
    ):
        self.authenticate_calls += 1

        return AuthenticatedAuthorization(
            flow_token=flow_token,
            user_id=uuid4(),
            memberships=(
                MembershipResponse(
                    business_id=self.business_id,
                    business_name="<Empresa A>",
                    role=MembershipRole.OWNER,
                ),
            ),
        )

    async def authorize_business(
        self,
        *,
        flow_token,
        business_id,
        issuer,
    ):
        self.authorize_calls += 1

        assert business_id == self.business_id

        return AuthorizationCompletion(
            code="authorization-code",
            state="state-123",
            redirect_uri=CHATGPT_REDIRECT_URI,
        )

    async def deny(
        self,
        *,
        flow_token: str,
    ):
        self.deny_calls += 1

        return AuthorizationDenial(
            state="state-123",
            redirect_uri=CHATGPT_REDIRECT_URI,
        )


class FakeLifecycle:
    def __init__(self) -> None:
        self.code_calls = 0
        self.refresh_calls = 0

    async def exchange_authorization_code(
        self,
        request,
    ):
        self.code_calls += 1

        return OAuthTokenSet(
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_in=600,
            scope="alovia.read",
        )

    async def refresh(
        self,
        request,
    ):
        self.refresh_calls += 1

        return OAuthTokenSet(
            access_token="access-token-2",
            refresh_token="refresh-token-2",
            token_type="Bearer",
            expires_in=600,
            scope="alovia.read",
        )


def _authorize_params() -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": CHATGPT_CLIENT_ID,
        "redirect_uri": CHATGPT_REDIRECT_URI,
        "state": "state-123",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "alovia.read offline_access",
        "resource": RESOURCE,
    }


def _test_client():
    coordinator = FakeCoordinator()
    lifecycle = FakeLifecycle()
    verifier = FakeMetadataVerifier()

    services = OAuthHTTPServices(
        coordinator=coordinator,
        lifecycle=lifecycle,
    )

    async def dependency():
        return services

    app = FastAPI()
    app.include_router(
        build_oauth_router(
            issuer=ISSUER,
            resource=RESOURCE,
            signing_key=SIGNING_KEY,
            metadata_verifier=verifier,
            services_dependency=dependency,
        )
    )

    return (
        TestClient(
            app,
            base_url="https://backend.example.com",
        ),
        coordinator,
        lifecycle,
        verifier,
    )


def test_authorization_server_metadata() -> None:
    client, _, _, _ = _test_client()

    response = client.get(
        "/.well-known/oauth-authorization-server"
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["issuer"] == ISSUER
    assert payload["authorization_endpoint"] == (
        f"{ISSUER}/oauth/authorize"
    )
    assert payload["token_endpoint"] == (
        f"{ISSUER}/oauth/token"
    )
    assert (
        payload[
            "authorization_response_iss_parameter_supported"
        ]
        is True
    )
    assert (
        payload[
            "client_id_metadata_document_supported"
        ]
        is True
    )
    assert payload[
        "code_challenge_methods_supported"
    ] == ["S256"]

    assert response.headers["cache-control"] == "no-store"


def test_authorize_validates_cimd_before_flow() -> None:
    client, coordinator, _, verifier = (
        _test_client()
    )

    response = client.get(
        "/oauth/authorize",
        params=_authorize_params(),
    )

    assert response.status_code == 200
    assert verifier.calls == 1
    assert coordinator.begin_calls == 1
    assert "flow-token" in response.text
    assert 'type="password"' in response.text
    assert (
        response.headers["x-frame-options"]
        == "DENY"
    )


def test_invalid_client_does_not_start_flow() -> None:
    client, coordinator, _, verifier = (
        _test_client()
    )

    params = _authorize_params()
    params["client_id"] = (
        "https://evil.example/client.json"
    )

    response = client.get(
        "/oauth/authorize",
        params=params,
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_client"
    }
    assert verifier.calls == 0
    assert coordinator.begin_calls == 0


def test_login_returns_escaped_business_consent() -> None:
    client, coordinator, _, _ = _test_client()

    response = client.post(
        "/oauth/login",
        data={
            "flow_token": "flow-token",
            "email": "owner@example.com",
            "password": "correct-password",
        },
    )

    assert response.status_code == 200
    assert coordinator.authenticate_calls == 1
    assert "&lt;Empresa A&gt;" in response.text
    assert "<Empresa A>" not in response.text
    assert str(coordinator.business_id) in response.text


def test_approve_redirect_contains_code_state_and_iss() -> None:
    client, coordinator, _, _ = _test_client()

    response = client.post(
        "/oauth/consent",
        data={
            "flow_token": "flow-token",
            "business_id": str(
                coordinator.business_id
            ),
            "action": "approve",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert coordinator.authorize_calls == 1

    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)

    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        == CHATGPT_REDIRECT_URI
    )
    assert query["code"] == [
        "authorization-code"
    ]
    assert query["state"] == ["state-123"]
    assert query["iss"] == [ISSUER]


def test_denial_redirect_contains_iss() -> None:
    client, coordinator, _, _ = _test_client()

    response = client.post(
        "/oauth/consent",
        data={
            "flow_token": "flow-token",
            "action": "deny",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert coordinator.deny_calls == 1

    query = parse_qs(
        urlsplit(
            response.headers["location"]
        ).query
    )

    assert query["error"] == ["access_denied"]
    assert query["state"] == ["state-123"]
    assert query["iss"] == [ISSUER]


def test_authorization_code_token_exchange() -> None:
    client, _, lifecycle, _ = _test_client()

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CHATGPT_CLIENT_ID,
            "resource": RESOURCE,
            "code": "authorization-code",
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "code_verifier": VERIFIER,
        },
    )

    assert response.status_code == 200
    assert lifecycle.code_calls == 1
    assert lifecycle.refresh_calls == 0

    assert response.json() == {
        "access_token": "access-token",
        "token_type": "Bearer",
        "expires_in": 600,
        "refresh_token": "refresh-token",
        "scope": "alovia.read",
    }

    assert response.headers["cache-control"] == "no-store"


def test_refresh_token_exchange() -> None:
    client, _, lifecycle, _ = _test_client()

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_CLIENT_ID,
            "resource": RESOURCE,
            "refresh_token": "refresh-token",
        },
    )

    assert response.status_code == 200
    assert lifecycle.code_calls == 0
    assert lifecycle.refresh_calls == 1

    payload = response.json()

    assert payload["access_token"] == (
        "access-token-2"
    )
    assert payload["refresh_token"] == (
        "refresh-token-2"
    )


def test_wrong_resource_fails_before_lifecycle() -> None:
    client, _, lifecycle, _ = _test_client()

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CHATGPT_CLIENT_ID,
            "resource": "https://evil.example/mcp-v2",
            "refresh_token": "refresh-token",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_target"
    }
    assert lifecycle.code_calls == 0
    assert lifecycle.refresh_calls == 0


def test_oauth_is_disabled_by_default(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ENVIRONMENT",
        "test",
    )
    monkeypatch.delenv(
        "MCP_OAUTH_ENABLED",
        raising=False,
    )
    monkeypatch.delenv(
        "MCP_OAUTH_SIGNING_KEY",
        raising=False,
    )

    get_settings.cache_clear()

    try:
        application = create_app()

        with TestClient(application) as client:
            response = client.get(
                "/.well-known/oauth-authorization-server"
            )

        assert response.status_code == 404
    finally:
        get_settings.cache_clear()


def test_oauth_enabled_without_signing_key_fails_closed(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ENVIRONMENT",
        "test",
    )
    monkeypatch.setenv(
        "MCP_OAUTH_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "MCP_V2_ISSUER_URL",
        ISSUER,
    )
    monkeypatch.setenv(
        "MCP_V2_RESOURCE_SERVER_URL",
        RESOURCE,
    )
    monkeypatch.delenv(
        "MCP_OAUTH_SIGNING_KEY",
        raising=False,
    )

    get_settings.cache_clear()

    try:
        try:
            create_app()
        except MCPConfigurationError as exc:
            assert "signing key" in str(exc)
        else:
            raise AssertionError(
                "OAuth startup should fail closed"
            )
    finally:
        get_settings.cache_clear()

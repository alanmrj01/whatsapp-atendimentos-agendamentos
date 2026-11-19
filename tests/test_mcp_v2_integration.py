from __future__ import annotations

import pytest
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.mcp_server.runtime import MCPConfigurationError


RESOURCE = "https://testserver/mcp-v2"
ISSUER = "https://auth.example.com"

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {
            "name": "alovia-mcp-v2-integration-test",
            "version": "1.0",
        },
    },
}


class StaticV2TokenVerifier:
    async def verify_token(
        self,
        token: str,
    ) -> AccessToken | None:
        if token != "valid":
            return None

        return AccessToken(
            token=token,
            client_id="chatgpt-test",
            scopes=["alovia.read"],
            resource=RESOURCE,
            subject="user-test",
            claims={"iss": ISSUER},
        )


def _reset_settings() -> None:
    get_settings.cache_clear()


def _configure_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_V2_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_V2_ALLOWED_HOSTS",
        "testserver,testserver:*",
    )
    monkeypatch.setenv(
        "MCP_V2_ISSUER_URL",
        ISSUER,
    )
    monkeypatch.setenv(
        "MCP_V2_RESOURCE_SERVER_URL",
        RESOURCE,
    )


def test_v2_disabled_by_default_preserves_v1_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS",
        "testserver,testserver:*",
    )
    monkeypatch.delenv("MCP_V2_ENABLED", raising=False)
    monkeypatch.delenv("MCP_V2_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_V2_ISSUER_URL", raising=False)
    monkeypatch.delenv(
        "MCP_V2_RESOURCE_SERVER_URL",
        raising=False,
    )
    _reset_settings()

    try:
        application = create_app()

        with TestClient(
            application,
            base_url="https://testserver",
        ) as client:
            v1 = client.post(
                "/mcp/",
                headers={
                    "Accept": (
                        "application/json, text/event-stream"
                    ),
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )
            v2 = client.post(
                "/mcp-v2",
                headers={
                    "Accept": (
                        "application/json, text/event-stream"
                    ),
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )
            health = client.get("/health")

        assert v1.status_code == 200
        assert (
            v1.json()["result"]["serverInfo"]["version"]
            == "1.0.0"
        )
        assert v2.status_code == 404
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
    finally:
        _reset_settings()


def test_v2_enabled_without_verifier_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_v2(monkeypatch)
    _reset_settings()

    try:
        with pytest.raises(
            MCPConfigurationError,
            match="token verifier",
        ):
            create_app()
    finally:
        _reset_settings()


@pytest.mark.parametrize(
    "missing_variable",
    (
        "MCP_V2_ALLOWED_HOSTS",
        "MCP_V2_ISSUER_URL",
        "MCP_V2_RESOURCE_SERVER_URL",
    ),
)
def test_v2_incomplete_oauth_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    missing_variable: str,
) -> None:
    _configure_v2(monkeypatch)
    monkeypatch.delenv(missing_variable, raising=False)
    _reset_settings()

    try:
        with pytest.raises(MCPConfigurationError):
            create_app(
                mcp_v2_token_verifier=StaticV2TokenVerifier()
            )
    finally:
        _reset_settings()


def test_v2_mount_exposes_root_metadata_and_requires_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS",
        "testserver,testserver:*",
    )
    _configure_v2(monkeypatch)
    _reset_settings()

    try:
        application = create_app(
            mcp_v2_token_verifier=StaticV2TokenVerifier()
        )

        with TestClient(
            application,
            base_url="https://testserver",
        ) as client:
            metadata = client.get(
                "/.well-known/oauth-protected-resource/mcp-v2"
            )

            unauthorized = client.post(
                "/mcp-v2",
                headers={
                    "Accept": (
                        "application/json, text/event-stream"
                    ),
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

            authorized = client.post(
                "/mcp-v2",
                headers={
                    "Authorization": "Bearer valid",
                    "Accept": (
                        "application/json, text/event-stream"
                    ),
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

            v1 = client.post(
                "/mcp/",
                headers={
                    "Accept": (
                        "application/json, text/event-stream"
                    ),
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

            health = client.get("/health")

        assert metadata.status_code == 200
        payload = metadata.json()
        assert payload["resource"].rstrip("/") == RESOURCE
        assert [
            value.rstrip("/")
            for value in payload["authorization_servers"]
        ] == [ISSUER]
        assert payload["scopes_supported"] == [
            "alovia.read"
        ]

        assert unauthorized.status_code == 401
        authenticate = unauthorized.headers[
            "www-authenticate"
        ]
        assert 'error="invalid_token"' in authenticate
        assert (
            'resource_metadata="'
            "https://testserver/"
            ".well-known/oauth-protected-resource/mcp-v2"
            '"'
            in authenticate
        )

        assert authorized.status_code == 200
        assert (
            authorized.json()["result"]["serverInfo"][
                "version"
            ]
            == "2.0.0"
        )

        assert v1.status_code == 200
        assert (
            v1.json()["result"]["serverInfo"]["version"]
            == "1.0.0"
        )

        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
    finally:
        _reset_settings()

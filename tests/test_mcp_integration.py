from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.mcp_server.runtime import MCPConfigurationError


INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {
            "name": "alovia-mcp-integration-test",
            "version": "1.0",
        },
    },
}


def _reset_settings() -> None:
    get_settings.cache_clear()


def test_mcp_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    _reset_settings()

    try:
        application = create_app()

        with TestClient(
            application,
            base_url="http://testserver",
        ) as client:
            response = client.get("/mcp/")
            health = client.get("/health")

        assert response.status_code == 404
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
    finally:
        _reset_settings()


def test_mcp_enabled_requires_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "")
    _reset_settings()

    try:
        with pytest.raises(MCPConfigurationError):
            create_app()
    finally:
        _reset_settings()


def test_mcp_mount_runs_session_manager_and_preserves_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS",
        "testserver,testserver:*",
    )
    _reset_settings()

    try:
        application = create_app()

        with TestClient(
            application,
            base_url="http://testserver",
        ) as client:
            response = client.post(
                "/mcp/",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )
            health = client.get("/health")

        assert response.status_code == 200

        payload = response.json()
        assert payload["result"]["serverInfo"]["name"] == "alovia"
        assert payload["result"]["serverInfo"]["version"] == "1.0.0"

        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
    finally:
        _reset_settings()


def test_mcp_rejects_unlisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS",
        "testserver,testserver:*",
    )
    _reset_settings()

    try:
        application = create_app()

        with TestClient(
            application,
            base_url="http://testserver",
        ) as client:
            response = client.post(
                "/mcp/",
                headers={
                    "Host": "evil.example",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

        assert response.status_code == 421
    finally:
        _reset_settings()

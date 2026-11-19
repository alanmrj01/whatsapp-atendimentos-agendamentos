from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

import app.main as main_module
from app.core.config import get_settings
from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
)
from app.mcp_server.oauth_verifier import (
    OAuthTokenPrincipal,
    SQLAlchemyMcpTokenVerifier,
)


ISSUER = "https://backend.example.com"
RESOURCE = "https://backend.example.com/mcp-v2"
SIGNING_KEY = "v" * 64


class FakeAuthorizationStore:
    def __init__(
        self,
        active: bool = True,
    ) -> None:
        self.active = active
        self.calls: list[
            OAuthTokenPrincipal
        ] = []

    async def is_active(
        self,
        principal: OAuthTokenPrincipal,
        *,
        now: datetime,
    ) -> bool:
        self.calls.append(principal)
        assert now.tzinfo is not None
        return self.active


def _unused_session_factory():
    raise AssertionError(
        "database must not be used by unit verifier"
    )


def _verifier(
    *,
    active: bool = True,
):
    verifier = SQLAlchemyMcpTokenVerifier(
        issuer=ISSUER,
        resource=RESOURCE,
        signing_key=SIGNING_KEY,
        session_factory_provider=(
            _unused_session_factory
        ),
    )

    store = FakeAuthorizationStore(
        active=active
    )
    verifier.store = store

    return verifier, store


def _claims(
    **overrides,
):
    now = datetime.now(UTC)

    values = {
        "iss": ISSUER,
        "sub": str(uuid4()),
        "aud": RESOURCE,
        "exp": now + timedelta(minutes=10),
        "iat": now,
        "jti": str(uuid4()),
        "client_id": CHATGPT_CLIENT_ID,
        "azp": CHATGPT_CLIENT_ID,
        "business_id": str(uuid4()),
        "sid": str(uuid4()),
        "scope": "alovia.read",
    }

    values.update(overrides)
    return values


def _token(
    *,
    key: str = SIGNING_KEY,
    **overrides,
) -> str:
    return jwt.encode(
        _claims(**overrides),
        key,
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_valid_token_returns_mcp_access_token() -> None:
    verifier, store = _verifier()

    raw = _token()

    access = await verifier.verify_token(raw)

    assert access is not None
    assert access.token == raw
    assert access.client_id == CHATGPT_CLIENT_ID
    assert access.scopes == ["alovia.read"]
    assert access.resource == RESOURCE
    assert access.subject is not None
    assert access.expires_at is not None

    assert access.claims is not None
    assert access.claims["iss"] == ISSUER
    assert "business_id" in access.claims
    assert "sid" in access.claims

    assert len(store.calls) == 1

    principal = store.calls[0]

    assert (
        str(principal.user_id)
        == access.subject
    )
    assert principal.client_id == CHATGPT_CLIENT_ID
    assert principal.scopes == (
        "alovia.read",
    )


@pytest.mark.asyncio
async def test_revoked_or_invalid_db_binding_rejects_token() -> None:
    verifier, store = _verifier(
        active=False
    )

    access = await verifier.verify_token(
        _token()
    )

    assert access is None
    assert len(store.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    (
        {"iss": "https://evil.example.com"},
        {"aud": "https://evil.example.com/mcp-v2"},
        {
            "exp": (
                datetime.now(UTC)
                - timedelta(seconds=1)
            )
        },
        {
            "client_id": (
                "https://evil.example/client.json"
            )
        },
        {
            "azp": (
                "https://evil.example/client.json"
            )
        },
        {"scope": "offline_access"},
        {
            "scope": (
                "alovia.read offline_access"
            )
        },
        {"sub": "not-a-uuid"},
        {"business_id": "not-a-uuid"},
        {"sid": "not-a-uuid"},
        {"jti": "not-a-uuid"},
    ),
)
async def test_invalid_claim_contract_is_rejected(
    overrides,
) -> None:
    verifier, store = _verifier()

    access = await verifier.verify_token(
        _token(**overrides)
    )

    assert access is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_wrong_signature_is_rejected_before_db() -> None:
    verifier, store = _verifier()

    access = await verifier.verify_token(
        _token(
            key="x" * 64
        )
    )

    assert access is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_missing_required_claim_is_rejected() -> None:
    verifier, store = _verifier()

    claims = _claims()
    claims.pop("business_id")

    raw = jwt.encode(
        claims,
        SIGNING_KEY,
        algorithm="HS256",
    )

    access = await verifier.verify_token(raw)

    assert access is None
    assert store.calls == []


@pytest.mark.asyncio
async def test_empty_or_oversized_token_is_rejected() -> None:
    verifier, store = _verifier()

    assert (
        await verifier.verify_token("")
        is None
    )
    assert (
        await verifier.verify_token(
            "x" * 9000
        )
        is None
    )

    assert store.calls == []


def test_short_signing_key_fails_closed() -> None:
    with pytest.raises(
        ValueError,
        match="too short",
    ):
        SQLAlchemyMcpTokenVerifier(
            issuer=ISSUER,
            resource=RESOURCE,
            signing_key="short",
            session_factory_provider=(
                _unused_session_factory
            ),
        )


def test_main_auto_wires_real_verifier_when_oauth_and_v2_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ENVIRONMENT",
        "test",
    )
    monkeypatch.setenv(
        "MCP_V2_ENABLED",
        "true",
    )
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
    monkeypatch.setenv(
        "MCP_OAUTH_ENABLED",
        "true",
    )
    monkeypatch.setenv(
        "MCP_OAUTH_SIGNING_KEY",
        SIGNING_KEY,
    )

    captured = {}

    original = (
        main_module.build_mcp_v2_runtime
    )

    def capture_runtime(**kwargs):
        captured["verifier"] = kwargs[
            "token_verifier"
        ]
        return original(**kwargs)

    monkeypatch.setattr(
        main_module,
        "build_mcp_v2_runtime",
        capture_runtime,
    )

    get_settings.cache_clear()

    try:
        application = main_module.create_app()

        assert (
            application.state.mcp_v2_runtime
            is not None
        )

        assert isinstance(
            captured["verifier"],
            SQLAlchemyMcpTokenVerifier,
        )
    finally:
        get_settings.cache_clear()


def test_v2_without_oauth_or_injected_verifier_still_fails_closed(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ENVIRONMENT",
        "test",
    )
    monkeypatch.setenv(
        "MCP_V2_ENABLED",
        "true",
    )
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
    monkeypatch.setenv(
        "MCP_OAUTH_ENABLED",
        "false",
    )
    monkeypatch.delenv(
        "MCP_OAUTH_SIGNING_KEY",
        raising=False,
    )

    get_settings.cache_clear()

    try:
        with pytest.raises(
            main_module.MCPConfigurationError,
            match="token verifier",
        ):
            main_module.create_app()
    finally:
        get_settings.cache_clear()


def test_short_oauth_key_fails_startup(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ENVIRONMENT",
        "test",
    )
    monkeypatch.setenv(
        "MCP_V2_ENABLED",
        "false",
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
    monkeypatch.setenv(
        "MCP_OAUTH_SIGNING_KEY",
        "short",
    )

    get_settings.cache_clear()

    try:
        with pytest.raises(
            main_module.MCPConfigurationError,
            match="signing key",
        ):
            main_module.create_app()
    finally:
        get_settings.cache_clear()

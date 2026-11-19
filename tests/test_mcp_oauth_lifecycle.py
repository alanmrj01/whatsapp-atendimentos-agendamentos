from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.auth.security import token_hash
from app.mcp_server.oauth_lifecycle import (
    ACCESS_TOKEN_TTL_SECONDS,
    OAuthGrantError,
    OAuthLifecycle,
)
from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    CHATGPT_REDIRECT_URI,
    AuthorizationRequest,
    TokenRequest,
)
from app.models import (
    McpOAuthAuthorizationCode,
    McpOAuthSession,
)


ISSUER = "https://backend.example.com"
RESOURCE = "https://backend.example.com/mcp-v2"
SIGNING_KEY = "k" * 64

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


class FakeStore:
    def __init__(self) -> None:
        self.codes: list[
            McpOAuthAuthorizationCode
        ] = []
        self.sessions: list[
            McpOAuthSession
        ] = []
        self.commit_count = 0

    async def add_authorization_code(
        self,
        item: McpOAuthAuthorizationCode,
    ) -> None:
        self.codes.append(item)

    async def authorization_code_for_update(
        self,
        code_hash_value: str,
    ) -> McpOAuthAuthorizationCode | None:
        return next(
            (
                item
                for item in self.codes
                if item.code_hash
                == code_hash_value
            ),
            None,
        )

    async def add_session(
        self,
        item: McpOAuthSession,
    ) -> None:
        self.sessions.append(item)

    async def session_for_update(
        self,
        refresh_hash: str,
    ) -> McpOAuthSession | None:
        return next(
            (
                item
                for item in self.sessions
                if item.refresh_token_hash
                == refresh_hash
            ),
            None,
        )

    async def commit(self) -> None:
        self.commit_count += 1


def _authorization_request() -> AuthorizationRequest:
    return AuthorizationRequest(
        client_id=CHATGPT_CLIENT_ID,
        redirect_uri=CHATGPT_REDIRECT_URI,
        state="state-123",
        code_challenge=CHALLENGE,
        scopes=(
            "alovia.read",
            "offline_access",
        ),
        resource=RESOURCE,
    )


def _lifecycle(
    store: FakeStore,
) -> OAuthLifecycle:
    return OAuthLifecycle(
        store=store,
        issuer=ISSUER,
        resource=RESOURCE,
        signing_key=SIGNING_KEY,
    )


async def _issue(
    lifecycle: OAuthLifecycle,
):
    return await lifecycle.issue_authorization_code(
        user_id=uuid4(),
        business_id=uuid4(),
        request=_authorization_request(),
    )


@pytest.mark.asyncio
async def test_authorization_code_is_only_stored_hashed() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    assert len(store.codes) == 1
    saved = store.codes[0]

    assert saved.code_hash == token_hash(
        issued.code
    )
    assert saved.code_hash != issued.code
    assert saved.used_at is None
    assert store.commit_count == 1


@pytest.mark.asyncio
async def test_code_exchange_binds_user_business_and_resource() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    user_id = uuid4()
    business_id = uuid4()

    issued = await lifecycle.issue_authorization_code(
        user_id=user_id,
        business_id=business_id,
        request=_authorization_request(),
    )

    result = await lifecycle.exchange_authorization_code(
        TokenRequest(
            grant_type="authorization_code",
            client_id=CHATGPT_CLIENT_ID,
            resource=RESOURCE,
            code=issued.code,
            redirect_uri=CHATGPT_REDIRECT_URI,
            code_verifier=VERIFIER,
        )
    )

    assert store.codes[0].used_at is not None
    assert len(store.sessions) == 1

    session = store.sessions[0]

    assert session.user_id == user_id
    assert session.business_id == business_id
    assert session.client_id == CHATGPT_CLIENT_ID
    assert session.resource == RESOURCE

    assert session.refresh_token_hash == token_hash(
        result.refresh_token
    )
    assert (
        session.refresh_token_hash
        != result.refresh_token
    )

    claims = jwt.decode(
        result.access_token,
        SIGNING_KEY,
        algorithms=["HS256"],
        audience=RESOURCE,
        issuer=ISSUER,
        options={
            "require": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "jti",
                "client_id",
                "business_id",
                "sid",
                "scope",
            ]
        },
    )

    assert claims["sub"] == str(user_id)
    assert claims["business_id"] == str(
        business_id
    )
    assert claims["client_id"] == CHATGPT_CLIENT_ID
    assert claims["azp"] == CHATGPT_CLIENT_ID
    assert claims["scope"] == "alovia.read"
    assert result.scope == "alovia.read"
    assert result.expires_in == ACCESS_TOKEN_TTL_SECONDS


@pytest.mark.asyncio
async def test_authorization_code_is_single_use() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    request = TokenRequest(
        grant_type="authorization_code",
        client_id=CHATGPT_CLIENT_ID,
        resource=RESOURCE,
        code=issued.code,
        redirect_uri=CHATGPT_REDIRECT_URI,
        code_verifier=VERIFIER,
    )

    await lifecycle.exchange_authorization_code(
        request
    )

    with pytest.raises(
        OAuthGrantError,
        match="Invalid authorization grant",
    ):
        await lifecycle.exchange_authorization_code(
            request
        )

    assert len(store.sessions) == 1


@pytest.mark.asyncio
async def test_wrong_pkce_cannot_consume_code() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    wrong_verifier = (
        "Z" * 43
    )

    with pytest.raises(OAuthGrantError) as exc:
        await lifecycle.exchange_authorization_code(
            TokenRequest(
                grant_type="authorization_code",
                client_id=CHATGPT_CLIENT_ID,
                resource=RESOURCE,
                code=issued.code,
                redirect_uri=CHATGPT_REDIRECT_URI,
                code_verifier=wrong_verifier,
            )
        )

    assert exc.value.error == "invalid_grant"
    assert store.codes[0].used_at is None
    assert store.sessions == []


@pytest.mark.asyncio
async def test_expired_code_is_rejected() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    store.codes[0].expires_at = (
        datetime.now(UTC)
        - timedelta(seconds=1)
    )

    with pytest.raises(OAuthGrantError):
        await lifecycle.exchange_authorization_code(
            TokenRequest(
                grant_type="authorization_code",
                client_id=CHATGPT_CLIENT_ID,
                resource=RESOURCE,
                code=issued.code,
                redirect_uri=CHATGPT_REDIRECT_URI,
                code_verifier=VERIFIER,
            )
        )

    assert store.sessions == []


@pytest.mark.asyncio
async def test_refresh_rotates_token_and_replay_fails() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    initial = await lifecycle.exchange_authorization_code(
        TokenRequest(
            grant_type="authorization_code",
            client_id=CHATGPT_CLIENT_ID,
            resource=RESOURCE,
            code=issued.code,
            redirect_uri=CHATGPT_REDIRECT_URI,
            code_verifier=VERIFIER,
        )
    )

    old_refresh = initial.refresh_token
    old_hash = token_hash(old_refresh)

    replacement = await lifecycle.refresh(
        TokenRequest(
            grant_type="refresh_token",
            client_id=CHATGPT_CLIENT_ID,
            resource=RESOURCE,
            refresh_token=old_refresh,
        )
    )

    assert replacement.refresh_token != old_refresh
    assert (
        store.sessions[0].refresh_token_hash
        == token_hash(
            replacement.refresh_token
        )
    )
    assert (
        store.sessions[0].refresh_token_hash
        != old_hash
    )

    with pytest.raises(OAuthGrantError) as exc:
        await lifecycle.refresh(
            TokenRequest(
                grant_type="refresh_token",
                client_id=CHATGPT_CLIENT_ID,
                resource=RESOURCE,
                refresh_token=old_refresh,
            )
        )

    assert exc.value.error == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_cannot_escalate_scope() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    initial = await lifecycle.exchange_authorization_code(
        TokenRequest(
            grant_type="authorization_code",
            client_id=CHATGPT_CLIENT_ID,
            resource=RESOURCE,
            code=issued.code,
            redirect_uri=CHATGPT_REDIRECT_URI,
            code_verifier=VERIFIER,
        )
    )

    with pytest.raises(OAuthGrantError) as exc:
        await lifecycle.refresh(
            TokenRequest(
                grant_type="refresh_token",
                client_id=CHATGPT_CLIENT_ID,
                resource=RESOURCE,
                refresh_token=initial.refresh_token,
                scopes=(
                    "alovia.read",
                    "evil.scope",
                ),
            )
        )

    assert exc.value.error == "invalid_scope"


@pytest.mark.asyncio
async def test_expired_or_revoked_refresh_is_rejected() -> None:
    store = FakeStore()
    lifecycle = _lifecycle(store)

    issued = await _issue(lifecycle)

    initial = await lifecycle.exchange_authorization_code(
        TokenRequest(
            grant_type="authorization_code",
            client_id=CHATGPT_CLIENT_ID,
            resource=RESOURCE,
            code=issued.code,
            redirect_uri=CHATGPT_REDIRECT_URI,
            code_verifier=VERIFIER,
        )
    )

    session = store.sessions[0]
    session.revoked_at = datetime.now(UTC)

    with pytest.raises(OAuthGrantError):
        await lifecycle.refresh(
            TokenRequest(
                grant_type="refresh_token",
                client_id=CHATGPT_CLIENT_ID,
                resource=RESOURCE,
                refresh_token=initial.refresh_token,
            )
        )


def test_short_signing_key_fails_closed() -> None:
    with pytest.raises(
        ValueError,
        match="too short",
    ):
        OAuthLifecycle(
            store=FakeStore(),
            issuer=ISSUER,
            resource=RESOURCE,
            signing_key="short",
        )

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import token_hash
from app.mcp_server.oauth_protocol import (
    MCP_READ_SCOPE,
    AuthorizationRequest,
    TokenRequest,
    pkce_s256_matches,
    validate_issuer,
    validate_resource,
)
from app.models import (
    McpOAuthAuthorizationCode,
    McpOAuthSession,
)


AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 10 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


class OAuthGrantError(ValueError):
    def __init__(
        self,
        error: str,
        description: str,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass(frozen=True, slots=True)
class IssuedAuthorizationCode:
    code: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    scope: str


class OAuthStore(Protocol):
    async def add_authorization_code(
        self,
        item: McpOAuthAuthorizationCode,
    ) -> None: ...

    async def authorization_code_for_update(
        self,
        code_hash: str,
    ) -> McpOAuthAuthorizationCode | None: ...

    async def add_session(
        self,
        item: McpOAuthSession,
    ) -> None: ...

    async def session_for_update(
        self,
        refresh_token_hash: str,
    ) -> McpOAuthSession | None: ...

    async def commit(self) -> None: ...


class SQLAlchemyOAuthStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def add_authorization_code(
        self,
        item: McpOAuthAuthorizationCode,
    ) -> None:
        self.db.add(item)
        await self.db.flush()

    async def authorization_code_for_update(
        self,
        code_hash: str,
    ) -> McpOAuthAuthorizationCode | None:
        return await self.db.scalar(
            select(McpOAuthAuthorizationCode)
            .where(
                McpOAuthAuthorizationCode.code_hash
                == code_hash
            )
            .with_for_update()
        )

    async def add_session(
        self,
        item: McpOAuthSession,
    ) -> None:
        self.db.add(item)
        await self.db.flush()

    async def session_for_update(
        self,
        refresh_token_hash: str,
    ) -> McpOAuthSession | None:
        return await self.db.scalar(
            select(McpOAuthSession)
            .where(
                McpOAuthSession.refresh_token_hash
                == refresh_token_hash
            )
            .with_for_update()
        )

    async def commit(self) -> None:
        await self.db.commit()


class OAuthLifecycle:
    def __init__(
        self,
        *,
        store: OAuthStore,
        issuer: str,
        resource: str,
        signing_key: str,
    ) -> None:
        self.store = store
        self.issuer = validate_issuer(issuer)
        self.resource = validate_resource(resource)

        if len(signing_key) < 32:
            raise ValueError(
                "MCP OAuth signing key is too short"
            )

        self.signing_key = signing_key

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _opaque_token() -> str:
        return secrets.token_urlsafe(48)

    @staticmethod
    def _stored_scope(
        scopes: tuple[str, ...],
    ) -> str:
        return " ".join(scopes)

    @staticmethod
    def _scope_tuple(scope: str) -> tuple[str, ...]:
        return tuple(
            item
            for item in scope.split()
            if item
        )

    @staticmethod
    def _access_scope(
        scopes: tuple[str, ...],
    ) -> str:
        if MCP_READ_SCOPE not in scopes:
            raise OAuthGrantError(
                "invalid_scope",
                "Required scope is missing",
            )
        return MCP_READ_SCOPE

    async def issue_authorization_code(
        self,
        *,
        user_id: UUID,
        business_id: UUID,
        request: AuthorizationRequest,
    ) -> IssuedAuthorizationCode:
        if request.resource != self.resource:
            raise OAuthGrantError(
                "invalid_target",
                "Resource does not match",
            )

        now = self._now()
        expires_at = now + timedelta(
            seconds=AUTHORIZATION_CODE_TTL_SECONDS
        )

        raw_code = self._opaque_token()

        item = McpOAuthAuthorizationCode(
            id=uuid4(),
            user_id=user_id,
            business_id=business_id,
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            code_hash=token_hash(raw_code),
            code_challenge=request.code_challenge,
            scope=self._stored_scope(request.scopes),
            resource=request.resource,
            expires_at=expires_at,
        )

        await self.store.add_authorization_code(item)
        await self.store.commit()

        return IssuedAuthorizationCode(
            code=raw_code,
            expires_at=expires_at,
        )

    def _issue_access_token(
        self,
        *,
        session: McpOAuthSession,
        access_scope: str,
        now: datetime,
    ) -> str:
        return jwt.encode(
            {
                "iss": self.issuer,
                "sub": str(session.user_id),
                "aud": self.resource,
                "exp": now
                + timedelta(
                    seconds=ACCESS_TOKEN_TTL_SECONDS
                ),
                "iat": now,
                "jti": str(uuid4()),
                "client_id": session.client_id,
                "azp": session.client_id,
                "business_id": str(
                    session.business_id
                ),
                "sid": str(session.id),
                "scope": access_scope,
            },
            self.signing_key,
            algorithm="HS256",
        )

    async def exchange_authorization_code(
        self,
        request: TokenRequest,
    ) -> OAuthTokenSet:
        if (
            request.grant_type
            != "authorization_code"
            or request.code is None
            or request.redirect_uri is None
            or request.code_verifier is None
        ):
            raise OAuthGrantError(
                "invalid_grant",
                "Invalid authorization grant",
            )

        now = self._now()

        item = (
            await self.store.authorization_code_for_update(
                token_hash(request.code)
            )
        )

        if (
            item is None
            or item.used_at is not None
            or item.expires_at <= now
            or item.client_id != request.client_id
            or item.redirect_uri
            != request.redirect_uri
            or item.resource != request.resource
            or request.resource != self.resource
            or not pkce_s256_matches(
                request.code_verifier,
                item.code_challenge,
            )
        ):
            raise OAuthGrantError(
                "invalid_grant",
                "Invalid authorization grant",
            )

        granted_scopes = self._scope_tuple(
            item.scope
        )
        access_scope = self._access_scope(
            granted_scopes
        )

        raw_refresh = self._opaque_token()

        session = McpOAuthSession(
            id=uuid4(),
            user_id=item.user_id,
            business_id=item.business_id,
            client_id=item.client_id,
            refresh_token_hash=token_hash(
                raw_refresh
            ),
            scope=item.scope,
            resource=item.resource,
            expires_at=now
            + timedelta(
                seconds=REFRESH_TOKEN_TTL_SECONDS
            ),
        )

        item.used_at = now

        await self.store.add_session(session)

        access = self._issue_access_token(
            session=session,
            access_scope=access_scope,
            now=now,
        )

        await self.store.commit()

        return OAuthTokenSet(
            access_token=access,
            refresh_token=raw_refresh,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=access_scope,
        )

    async def refresh(
        self,
        request: TokenRequest,
    ) -> OAuthTokenSet:
        if (
            request.grant_type != "refresh_token"
            or request.refresh_token is None
        ):
            raise OAuthGrantError(
                "invalid_grant",
                "Invalid refresh grant",
            )

        now = self._now()

        session = await self.store.session_for_update(
            token_hash(request.refresh_token)
        )

        if (
            session is None
            or session.revoked_at is not None
            or session.expires_at <= now
            or session.client_id != request.client_id
            or session.resource != request.resource
            or request.resource != self.resource
        ):
            raise OAuthGrantError(
                "invalid_grant",
                "Invalid refresh grant",
            )

        granted = self._scope_tuple(
            session.scope
        )

        requested = (
            request.scopes
            if request.scopes
            else granted
        )

        if (
            not set(requested).issubset(
                set(granted)
            )
            or MCP_READ_SCOPE not in requested
        ):
            raise OAuthGrantError(
                "invalid_scope",
                "Requested scope is not granted",
            )

        access_scope = self._access_scope(
            requested
        )

        replacement = self._opaque_token()

        session.refresh_token_hash = token_hash(
            replacement
        )

        access = self._issue_access_token(
            session=session,
            access_scope=access_scope,
            now=now,
        )

        await self.store.commit()

        return OAuthTokenSet(
            access_token=access,
            refresh_token=replacement,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=access_scope,
        )

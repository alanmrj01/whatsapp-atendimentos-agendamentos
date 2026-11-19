from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    TokenVerifier,
)
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    MCP_READ_SCOPE,
    validate_issuer,
    validate_resource,
)
from app.models import (
    Business,
    BusinessUserMembership,
    McpOAuthSession,
    User,
)


MAX_ACCESS_TOKEN_LENGTH = 8192


@dataclass(frozen=True, slots=True)
class OAuthTokenPrincipal:
    user_id: UUID
    business_id: UUID
    session_id: UUID
    client_id: str
    scopes: tuple[str, ...]


class OAuthTokenAuthorizationStore:
    def __init__(
        self,
        session_factory_provider: Callable[
            [],
            async_sessionmaker[AsyncSession],
        ],
        *,
        resource: str,
    ) -> None:
        self.session_factory_provider = (
            session_factory_provider
        )
        self.resource = resource

    async def is_active(
        self,
        principal: OAuthTokenPrincipal,
        *,
        now: datetime,
    ) -> bool:
        factory = self.session_factory_provider()

        async with factory() as db:
            stored_scope = await db.scalar(
                select(McpOAuthSession.scope)
                .join(
                    User,
                    User.id
                    == McpOAuthSession.user_id,
                )
                .join(
                    Business,
                    Business.id
                    == McpOAuthSession.business_id,
                )
                .join(
                    BusinessUserMembership,
                    and_(
                        BusinessUserMembership.user_id
                        == McpOAuthSession.user_id,
                        BusinessUserMembership.business_id
                        == McpOAuthSession.business_id,
                    ),
                )
                .where(
                    McpOAuthSession.id
                    == principal.session_id,
                    McpOAuthSession.user_id
                    == principal.user_id,
                    McpOAuthSession.business_id
                    == principal.business_id,
                    McpOAuthSession.client_id
                    == principal.client_id,
                    McpOAuthSession.resource
                    == self.resource,
                    McpOAuthSession.revoked_at.is_(
                        None
                    ),
                    McpOAuthSession.expires_at
                    > now,
                    User.is_active.is_(True),
                    User.platform_role.is_(None),
                    Business.active.is_(True),
                )
            )

        if stored_scope is None:
            return False

        granted = {
            item
            for item in stored_scope.split()
            if item
        }

        return set(principal.scopes).issubset(
            granted
        )


class SQLAlchemyMcpTokenVerifier(TokenVerifier):
    def __init__(
        self,
        *,
        issuer: str,
        resource: str,
        signing_key: str,
        session_factory_provider: Callable[
            [],
            async_sessionmaker[AsyncSession],
        ],
    ) -> None:
        self.issuer = validate_issuer(issuer)
        self.resource = validate_resource(resource)

        if len(signing_key) < 32:
            raise ValueError(
                "MCP OAuth signing key is too short"
            )

        self.signing_key = signing_key
        self.store = OAuthTokenAuthorizationStore(
            session_factory_provider,
            resource=self.resource,
        )

    async def verify_token(
        self,
        token: str,
    ) -> AccessToken | None:
        if (
            not token
            or len(token) > MAX_ACCESS_TOKEN_LENGTH
        ):
            return None

        try:
            claims = jwt.decode(
                token,
                self.signing_key,
                algorithms=["HS256"],
                audience=self.resource,
                issuer=self.issuer,
                options={
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "exp",
                        "iat",
                        "jti",
                        "client_id",
                        "azp",
                        "business_id",
                        "sid",
                        "scope",
                    ]
                },
            )

            client_id = claims["client_id"]
            azp = claims["azp"]
            scope = claims["scope"]

            if (
                client_id != CHATGPT_CLIENT_ID
                or azp != CHATGPT_CLIENT_ID
                or not isinstance(scope, str)
            ):
                return None

            scopes = tuple(
                item
                for item in scope.split()
                if item
            )

            # The V2 access token contract currently
            # grants exactly one runtime permission.
            if scopes != (MCP_READ_SCOPE,):
                return None

            user_id = UUID(claims["sub"])
            business_id = UUID(
                claims["business_id"]
            )
            session_id = UUID(claims["sid"])

            # Validate shape/entropy-bearing identifier
            # even though it is not used as authority.
            UUID(claims["jti"])

            exp = claims["exp"]

            if not isinstance(exp, (int, float)):
                return None

            principal = OAuthTokenPrincipal(
                user_id=user_id,
                business_id=business_id,
                session_id=session_id,
                client_id=client_id,
                scopes=scopes,
            )

            active = await self.store.is_active(
                principal,
                now=datetime.now(UTC),
            )

            if not active:
                return None

            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=list(scopes),
                expires_at=int(exp),
                resource=self.resource,
                subject=str(user_id),
                claims={
                    "iss": self.issuer,
                    "business_id": str(
                        business_id
                    ),
                    "sid": str(session_id),
                },
            )

        except (
            jwt.PyJWTError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            return None

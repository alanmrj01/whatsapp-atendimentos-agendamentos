from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import MembershipResponse
from app.auth.security import (
    normalize_email,
    token_hash,
    verify_password,
)
from app.auth.service import AuthService
from app.mcp_server.oauth_lifecycle import (
    IssuedAuthorizationCode,
)
from app.mcp_server.oauth_protocol import (
    AuthorizationRequest,
)
from app.models import (
    McpOAuthAuthorizationFlow,
    User,
)


AUTHORIZATION_FLOW_TTL_SECONDS = 10 * 60


class AuthorizationFlowError(ValueError):
    def __init__(
        self,
        error: str,
        description: str,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    flow_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedAuthorization:
    flow_token: str
    user_id: UUID
    memberships: tuple[MembershipResponse, ...]


@dataclass(frozen=True, slots=True)
class AuthorizationCompletion:
    code: str
    state: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AuthorizationDenial:
    state: str
    redirect_uri: str


class AuthorizationFlowStore(Protocol):
    async def add_flow(
        self,
        flow: McpOAuthAuthorizationFlow,
    ) -> None: ...

    async def flow_for_update(
        self,
        flow_token_hash: str,
    ) -> McpOAuthAuthorizationFlow | None: ...

    async def user_by_email(
        self,
        email: str,
    ) -> User | None: ...

    async def user_by_id(
        self,
        user_id: UUID,
    ) -> User | None: ...

    async def memberships_for_user(
        self,
        user: User,
    ) -> list[MembershipResponse]: ...

    async def commit(self) -> None: ...


class AuthorizationCodeIssuer(Protocol):
    async def issue_authorization_code(
        self,
        *,
        user_id: UUID,
        business_id: UUID,
        request: AuthorizationRequest,
    ) -> IssuedAuthorizationCode: ...


class SQLAlchemyAuthorizationFlowStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def add_flow(
        self,
        flow: McpOAuthAuthorizationFlow,
    ) -> None:
        self.db.add(flow)
        await self.db.flush()

    async def flow_for_update(
        self,
        flow_token_hash: str,
    ) -> McpOAuthAuthorizationFlow | None:
        return await self.db.scalar(
            select(McpOAuthAuthorizationFlow)
            .where(
                McpOAuthAuthorizationFlow.flow_token_hash
                == flow_token_hash
            )
            .with_for_update()
        )

    async def user_by_email(
        self,
        email: str,
    ) -> User | None:
        return await self.db.scalar(
            select(User).where(User.email == email)
        )

    async def user_by_id(
        self,
        user_id: UUID,
    ) -> User | None:
        return await self.db.get(User, user_id)

    async def memberships_for_user(
        self,
        user: User,
    ) -> list[MembershipResponse]:
        return await AuthService(
            self.db
        ).memberships(user)

    async def commit(self) -> None:
        await self.db.commit()


class OAuthAuthorizationCoordinator:
    def __init__(
        self,
        *,
        store: AuthorizationFlowStore,
    ) -> None:
        self.store = store

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _new_flow_token() -> str:
        return secrets.token_urlsafe(48)

    def _require_active(
        self,
        flow: McpOAuthAuthorizationFlow | None,
    ) -> McpOAuthAuthorizationFlow:
        now = self._now()

        if (
            flow is None
            or flow.consumed_at is not None
            or flow.expires_at <= now
        ):
            raise AuthorizationFlowError(
                "invalid_request",
                "Authorization flow is invalid or expired",
            )

        return flow

    async def begin(
        self,
        request: AuthorizationRequest,
    ) -> PendingAuthorization:
        now = self._now()
        raw_token = self._new_flow_token()
        expires_at = now + timedelta(
            seconds=AUTHORIZATION_FLOW_TTL_SECONDS
        )

        flow = McpOAuthAuthorizationFlow(
            id=uuid4(),
            flow_token_hash=token_hash(raw_token),
            client_id=request.client_id,
            redirect_uri=request.redirect_uri,
            state=request.state,
            code_challenge=request.code_challenge,
            scope=" ".join(request.scopes),
            resource=request.resource,
            expires_at=expires_at,
        )

        await self.store.add_flow(flow)
        await self.store.commit()

        return PendingAuthorization(
            flow_token=raw_token,
            expires_at=expires_at,
        )

    async def authenticate(
        self,
        *,
        flow_token: str,
        email: str,
        password: str,
    ) -> AuthenticatedAuthorization:
        if not flow_token or len(flow_token) > 512:
            raise AuthorizationFlowError(
                "invalid_request",
                "Authorization flow is invalid or expired",
            )

        flow = self._require_active(
            await self.store.flow_for_update(
                token_hash(flow_token)
            )
        )

        try:
            normalized_email = normalize_email(email)
        except ValueError:
            normalized_email = ""

        user = (
            await self.store.user_by_email(
                normalized_email
            )
            if normalized_email
            else None
        )

        valid_password = await to_thread.run_sync(
            verify_password,
            password,
            user.password_hash if user else None,
        )

        if (
            not valid_password
            or user is None
            or not user.is_active
        ):
            raise AuthorizationFlowError(
                "access_denied",
                "Invalid email or password",
            )

        if user.platform_role == "super_admin":
            raise AuthorizationFlowError(
                "access_denied",
                "Platform administrators cannot authorize MCP",
            )

        if (
            flow.user_id is not None
            and flow.user_id != user.id
        ):
            raise AuthorizationFlowError(
                "access_denied",
                "Authorization identity mismatch",
            )

        memberships = await (
            self.store.memberships_for_user(user)
        )

        if not memberships:
            raise AuthorizationFlowError(
                "access_denied",
                "No authorized business is available",
            )

        flow.user_id = user.id

        await self.store.commit()

        return AuthenticatedAuthorization(
            flow_token=flow_token,
            user_id=user.id,
            memberships=tuple(memberships),
        )

    async def authorize_business(
        self,
        *,
        flow_token: str,
        business_id: UUID,
        issuer: AuthorizationCodeIssuer,
    ) -> AuthorizationCompletion:
        if not flow_token or len(flow_token) > 512:
            raise AuthorizationFlowError(
                "invalid_request",
                "Authorization flow is invalid or expired",
            )

        flow = self._require_active(
            await self.store.flow_for_update(
                token_hash(flow_token)
            )
        )

        if flow.user_id is None:
            raise AuthorizationFlowError(
                "access_denied",
                "Authentication is required",
            )

        user = await self.store.user_by_id(
            flow.user_id
        )

        if (
            user is None
            or not user.is_active
            or user.platform_role == "super_admin"
        ):
            raise AuthorizationFlowError(
                "access_denied",
                "Authorization identity is no longer valid",
            )

        memberships = await (
            self.store.memberships_for_user(user)
        )

        authorized_businesses = {
            membership.business_id
            for membership in memberships
        }

        if business_id not in authorized_businesses:
            raise AuthorizationFlowError(
                "access_denied",
                "Business access denied",
            )

        request = AuthorizationRequest(
            client_id=flow.client_id,
            redirect_uri=flow.redirect_uri,
            state=flow.state,
            code_challenge=flow.code_challenge,
            scopes=tuple(
                item
                for item in flow.scope.split()
                if item
            ),
            resource=flow.resource,
        )

        flow.business_id = business_id
        flow.consumed_at = self._now()

        issued = await issuer.issue_authorization_code(
            user_id=user.id,
            business_id=business_id,
            request=request,
        )

        return AuthorizationCompletion(
            code=issued.code,
            state=flow.state,
            redirect_uri=flow.redirect_uri,
        )

    async def deny(
        self,
        *,
        flow_token: str,
    ) -> AuthorizationDenial:
        if not flow_token or len(flow_token) > 512:
            raise AuthorizationFlowError(
                "invalid_request",
                "Authorization flow is invalid or expired",
            )

        flow = self._require_active(
            await self.store.flow_for_update(
                token_hash(flow_token)
            )
        )

        flow.consumed_at = self._now()
        await self.store.commit()

        return AuthorizationDenial(
            state=flow.state,
            redirect_uri=flow.redirect_uri,
        )

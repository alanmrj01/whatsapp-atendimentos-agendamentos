from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.auth.schemas import (
    MembershipResponse,
    MembershipRole,
)
from app.auth.security import (
    hash_password,
    token_hash,
)
from app.mcp_server.oauth_authorization import (
    AuthorizationFlowError,
    OAuthAuthorizationCoordinator,
)
from app.mcp_server.oauth_lifecycle import (
    IssuedAuthorizationCode,
)
from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    CHATGPT_REDIRECT_URI,
    AuthorizationRequest,
)
from app.models import (
    McpOAuthAuthorizationFlow,
    User,
)


RESOURCE = "https://backend.example.com/mcp-v2"

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
        self.flows: list[
            McpOAuthAuthorizationFlow
        ] = []
        self.users_by_email: dict[str, User] = {}
        self.memberships: dict[
            UUID,
            list[MembershipResponse],
        ] = {}
        self.commit_count = 0

    async def add_flow(
        self,
        flow: McpOAuthAuthorizationFlow,
    ) -> None:
        self.flows.append(flow)

    async def flow_for_update(
        self,
        flow_hash: str,
    ) -> McpOAuthAuthorizationFlow | None:
        return next(
            (
                flow
                for flow in self.flows
                if flow.flow_token_hash
                == flow_hash
            ),
            None,
        )

    async def user_by_email(
        self,
        email: str,
    ) -> User | None:
        return self.users_by_email.get(email)

    async def user_by_id(
        self,
        user_id: UUID,
    ) -> User | None:
        return next(
            (
                user
                for user in self.users_by_email.values()
                if user.id == user_id
            ),
            None,
        )

    async def memberships_for_user(
        self,
        user: User,
    ) -> list[MembershipResponse]:
        return list(
            self.memberships.get(user.id, [])
        )

    async def commit(self) -> None:
        self.commit_count += 1


class FakeIssuer:
    def __init__(self) -> None:
        self.calls: list[
            tuple[UUID, UUID, AuthorizationRequest]
        ] = []

    async def issue_authorization_code(
        self,
        *,
        user_id: UUID,
        business_id: UUID,
        request: AuthorizationRequest,
    ) -> IssuedAuthorizationCode:
        self.calls.append(
            (user_id, business_id, request)
        )

        return IssuedAuthorizationCode(
            code="issued-code",
            expires_at=(
                datetime.now(UTC)
                + timedelta(minutes=5)
            ),
        )


def _request() -> AuthorizationRequest:
    return AuthorizationRequest(
        client_id=CHATGPT_CLIENT_ID,
        redirect_uri=CHATGPT_REDIRECT_URI,
        state="chatgpt-state",
        code_challenge=CHALLENGE,
        scopes=(
            "alovia.read",
            "offline_access",
        ),
        resource=RESOURCE,
    )


def _user(
    *,
    email: str = "owner@example.com",
    password: str = "correct-password",
    platform_role: str | None = None,
) -> User:
    return User(
        id=uuid4(),
        email=email,
        password_hash=hash_password(password),
        is_active=True,
        platform_role=platform_role,
    )


def _membership(
    business_id: UUID,
    name: str,
) -> MembershipResponse:
    return MembershipResponse(
        business_id=business_id,
        business_name=name,
        role=MembershipRole.OWNER,
    )


@pytest.mark.asyncio
async def test_pending_flow_stores_only_token_hash() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    pending = await coordinator.begin(
        _request()
    )

    assert len(store.flows) == 1

    flow = store.flows[0]

    assert flow.flow_token_hash == token_hash(
        pending.flow_token
    )
    assert flow.flow_token_hash != pending.flow_token
    assert flow.user_id is None
    assert flow.business_id is None
    assert flow.consumed_at is None


@pytest.mark.asyncio
async def test_valid_login_binds_user_without_pwa_session() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    business_id = uuid4()

    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(
            business_id,
            "Empresa A",
        )
    ]

    pending = await coordinator.begin(
        _request()
    )

    authenticated = await coordinator.authenticate(
        flow_token=pending.flow_token,
        email=" OWNER@example.com ",
        password="correct-password",
    )

    assert authenticated.user_id == user.id
    assert authenticated.memberships[0].business_id == (
        business_id
    )
    assert store.flows[0].user_id == user.id
    assert store.flows[0].business_id is None


@pytest.mark.asyncio
async def test_wrong_password_does_not_bind_identity() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(uuid4(), "Empresa A")
    ]

    pending = await coordinator.begin(
        _request()
    )

    with pytest.raises(
        AuthorizationFlowError
    ) as exc:
        await coordinator.authenticate(
            flow_token=pending.flow_token,
            email=user.email,
            password="wrong-password",
        )

    assert exc.value.error == "access_denied"
    assert store.flows[0].user_id is None


@pytest.mark.asyncio
async def test_super_admin_cannot_authorize_mcp() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user(
        platform_role="super_admin"
    )

    store.users_by_email[user.email] = user

    pending = await coordinator.begin(
        _request()
    )

    with pytest.raises(
        AuthorizationFlowError
    ) as exc:
        await coordinator.authenticate(
            flow_token=pending.flow_token,
            email=user.email,
            password="correct-password",
        )

    assert exc.value.error == "access_denied"
    assert store.flows[0].user_id is None


@pytest.mark.asyncio
async def test_business_is_revalidated_server_side() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    business_a = uuid4()
    business_b = uuid4()

    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(business_a, "Empresa A"),
        _membership(business_b, "Empresa B"),
    ]

    pending = await coordinator.begin(
        _request()
    )

    authenticated = await coordinator.authenticate(
        flow_token=pending.flow_token,
        email=user.email,
        password="correct-password",
    )

    assert {
        item.business_id
        for item in authenticated.memberships
    } == {
        business_a,
        business_b,
    }

    issuer = FakeIssuer()

    completed = await coordinator.authorize_business(
        flow_token=pending.flow_token,
        business_id=business_b,
        issuer=issuer,
    )

    assert completed.code == "issued-code"
    assert completed.state == "chatgpt-state"
    assert len(issuer.calls) == 1

    issued_user, issued_business, request = (
        issuer.calls[0]
    )

    assert issued_user == user.id
    assert issued_business == business_b
    assert request.resource == RESOURCE
    assert store.flows[0].business_id == business_b
    assert store.flows[0].consumed_at is not None


@pytest.mark.asyncio
async def test_revoked_membership_between_login_and_consent_fails() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    business_id = uuid4()

    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(
            business_id,
            "Empresa A",
        )
    ]

    pending = await coordinator.begin(
        _request()
    )

    await coordinator.authenticate(
        flow_token=pending.flow_token,
        email=user.email,
        password="correct-password",
    )

    store.memberships[user.id] = []

    issuer = FakeIssuer()

    with pytest.raises(
        AuthorizationFlowError
    ) as exc:
        await coordinator.authorize_business(
            flow_token=pending.flow_token,
            business_id=business_id,
            issuer=issuer,
        )

    assert exc.value.error == "access_denied"
    assert issuer.calls == []
    assert store.flows[0].consumed_at is None


@pytest.mark.asyncio
async def test_arbitrary_business_id_is_rejected() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    authorized_business = uuid4()

    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(
            authorized_business,
            "Empresa A",
        )
    ]

    pending = await coordinator.begin(
        _request()
    )

    await coordinator.authenticate(
        flow_token=pending.flow_token,
        email=user.email,
        password="correct-password",
    )

    issuer = FakeIssuer()

    with pytest.raises(
        AuthorizationFlowError
    ) as exc:
        await coordinator.authorize_business(
            flow_token=pending.flow_token,
            business_id=uuid4(),
            issuer=issuer,
        )

    assert exc.value.error == "access_denied"
    assert issuer.calls == []
    assert store.flows[0].consumed_at is None


@pytest.mark.asyncio
async def test_consumed_flow_cannot_authorize_twice() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    user = _user()
    business_id = uuid4()

    store.users_by_email[user.email] = user
    store.memberships[user.id] = [
        _membership(
            business_id,
            "Empresa A",
        )
    ]

    pending = await coordinator.begin(
        _request()
    )

    await coordinator.authenticate(
        flow_token=pending.flow_token,
        email=user.email,
        password="correct-password",
    )

    issuer = FakeIssuer()

    await coordinator.authorize_business(
        flow_token=pending.flow_token,
        business_id=business_id,
        issuer=issuer,
    )

    with pytest.raises(
        AuthorizationFlowError
    ):
        await coordinator.authorize_business(
            flow_token=pending.flow_token,
            business_id=business_id,
            issuer=issuer,
        )

    assert len(issuer.calls) == 1


@pytest.mark.asyncio
async def test_expired_flow_fails_closed() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    pending = await coordinator.begin(
        _request()
    )

    store.flows[0].expires_at = (
        datetime.now(UTC)
        - timedelta(seconds=1)
    )

    with pytest.raises(
        AuthorizationFlowError
    ) as exc:
        await coordinator.authenticate(
            flow_token=pending.flow_token,
            email="owner@example.com",
            password="irrelevant",
        )

    assert exc.value.error == "invalid_request"


@pytest.mark.asyncio
async def test_denial_consumes_flow() -> None:
    store = FakeStore()
    coordinator = OAuthAuthorizationCoordinator(
        store=store
    )

    pending = await coordinator.begin(
        _request()
    )

    denial = await coordinator.deny(
        flow_token=pending.flow_token
    )

    assert denial.state == "chatgpt-state"
    assert (
        denial.redirect_uri
        == CHATGPT_REDIRECT_URI
    )
    assert store.flows[0].consumed_at is not None

    with pytest.raises(
        AuthorizationFlowError
    ):
        await coordinator.deny(
            flow_token=pending.flow_token
        )

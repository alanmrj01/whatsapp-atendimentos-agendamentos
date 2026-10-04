from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import MeResponse, MembershipResponse, MembershipRole, SignupRequest
from app.billing.entitlements import active_subscription_exists
from app.auth.security import (
    PASSWORD_RESET_TTL_SECONDS,
    REFRESH_TTL_SECONDS,
    hash_password,
    new_password_reset_token,
    new_refresh_token,
    token_hash,
    verify_password,
)
from app.models import (
    AuthSession,
    Business,
    BusinessAccess,
    BusinessUserMembership,
    CommercialSubscription,
    PasswordResetToken,
    User,
)
from app.models.billing import billing_provider_environment
from app.operations.defaults import default_services_for_business
from app.repositories.web_push import WebPushRepository


def unauthorized() -> HTTPException:
    return HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


@dataclass
class Principal:
    user: User
    session: AuthSession
    memberships: list[MembershipResponse]

    def active_membership(self) -> MembershipResponse:
        if self.user.platform_role == "super_admin":
            raise HTTPException(403, "Use the platform administration area")
        for membership in self.memberships:
            if membership.business_id == self.session.active_business_id:
                return membership
        raise HTTPException(403, "Select an authorized business")

    def view(self) -> MeResponse:
        authorized = {m.business_id for m in self.memberships}
        return MeResponse(
            id=self.user.id,
            email=self.user.email,
            platform_role=self.user.platform_role,
            active_business_id=(
                self.session.active_business_id
                if self.session.active_business_id in authorized
                else None
            ),
            memberships=self.memberships,
        )


@dataclass(frozen=True, slots=True)
class PasswordResetIssue:
    id: UUID
    email: str
    token: str


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.billing_environment = billing_provider_environment()

    async def memberships(self, user: User) -> list[MembershipResponse]:
        if user.platform_role == "super_admin":
            return []

        commercial_access = active_subscription_exists(
            Business.id,
            self.billing_environment,
        )
        effective_access = case(
            (
                or_(
                    func.coalesce(BusinessAccess.access_mode, "paid") == "paid",
                    func.coalesce(BusinessAccess.admin_full_access, False),
                    commercial_access,
                ),
                "paid",
            ),
            else_="free",
        )
        rows = await self.db.execute(
            select(
                BusinessUserMembership,
                Business.name,
                effective_access,
                # Missing legacy access rows were historically treated as paid,
                # so they must also be treated as having real operational history.
                func.coalesce(BusinessAccess.has_had_operational_access, True),
                func.coalesce(BusinessAccess.admin_full_access, False),
            )
            .join(Business, Business.id == BusinessUserMembership.business_id)
            .outerjoin(BusinessAccess, BusinessAccess.business_id == Business.id)
            .where(BusinessUserMembership.user_id == user.id, Business.active.is_(True))
            .order_by(Business.name, Business.id)
        )
        return [
            MembershipResponse(
                business_id=m.business_id,
                business_name=name,
                role=MembershipRole(m.role),
                access_mode=access_mode,
                has_had_operational_access=has_had_operational_access,
                admin_full_access=admin_full_access,
                account_state=(
                    "active"
                    if access_mode == "paid"
                    else (
                        "payment_blocked"
                        if has_had_operational_access
                        else "demo"
                    )
                ),
            )
            for (
                m,
                name,
                access_mode,
                has_had_operational_access,
                admin_full_access,
            ) in rows
        ]

    async def _select_default(self, user: User, session: AuthSession) -> None:
        memberships = await self.memberships(user)
        if session.active_business_id not in {m.business_id for m in memberships}:
            session.active_business_id = memberships[0].business_id if len(memberships) == 1 else None

    @staticmethod
    def _new_session(user_id: UUID, business_id: UUID | None = None) -> tuple[AuthSession, str]:
        refresh = new_refresh_token()
        return (
            AuthSession(
                user_id=user_id,
                active_business_id=business_id,
                refresh_token_hash=token_hash(refresh),
                expires_at=datetime.now(UTC) + timedelta(seconds=REFRESH_TTL_SECONDS),
            ),
            refresh,
        )

    async def _revoke_sessions(
        self,
        user_id: UUID,
        *,
        keep_session_id: UUID | None = None,
    ) -> None:
        sessions = list(
            (
                await self.db.scalars(
                    select(AuthSession)
                    .where(
                        AuthSession.user_id == user_id,
                        AuthSession.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
        )
        revoked_at = datetime.now(UTC)
        push = WebPushRepository(self.db)
        for session in sessions:
            if keep_session_id is not None and session.id == keep_session_id:
                continue
            await push.remove_for_auth_session(session.id)
            session.revoked_at = revoked_at

    async def _revoke_password_reset_tokens(
        self,
        user_id: UUID,
        *,
        keep_token_id: UUID | None = None,
    ) -> None:
        statement = update(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.revoked_at.is_(None),
        )
        if keep_token_id is not None:
            statement = statement.where(PasswordResetToken.id != keep_token_id)
        await self.db.execute(
            statement.values(revoked_at=datetime.now(UTC))
        )

    async def issue_password_reset(self, email: str) -> PasswordResetIssue | None:
        user = await self.db.scalar(
            select(User)
            .where(User.email == email, User.is_active.is_(True))
            .with_for_update()
        )
        if user is None:
            return None
        await self._revoke_password_reset_tokens(user.id)
        raw_token = new_password_reset_token()
        reset = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash(raw_token),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=PASSWORD_RESET_TTL_SECONDS),
        )
        self.db.add(reset)
        await self.db.flush()
        issue = PasswordResetIssue(
            id=reset.id,
            email=user.email,
            token=raw_token,
        )
        await self.db.commit()
        return issue

    async def revoke_password_reset(self, reset_id: UUID) -> None:
        reset = await self.db.get(PasswordResetToken, reset_id)
        if reset is not None and reset.used_at is None and reset.revoked_at is None:
            reset.revoked_at = datetime.now(UTC)
            await self.db.commit()

    async def change_password(
        self,
        principal: Principal,
        current_password: str,
        new_password: str,
    ) -> None:
        user = await self.db.scalar(
            select(User)
            .where(User.id == principal.user.id, User.is_active.is_(True))
            .with_for_update()
        )
        if user is None:
            raise unauthorized()
        current_session = await self.db.scalar(
            select(AuthSession)
            .where(
                AuthSession.id == principal.session.id,
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
        if current_session is None:
            raise unauthorized()
        current_valid = await to_thread.run_sync(
            verify_password,
            current_password,
            user.password_hash,
        )
        if not current_valid:
            raise HTTPException(400, "Current password is incorrect")
        same_password = await to_thread.run_sync(
            verify_password,
            new_password,
            user.password_hash,
        )
        if same_password:
            raise HTTPException(400, "New password must be different")
        user.password_hash = await to_thread.run_sync(
            hash_password,
            new_password,
        )
        await self._revoke_password_reset_tokens(user.id)
        await self._revoke_sessions(
            user.id,
            keep_session_id=principal.session.id,
        )
        await self.db.commit()

    async def reset_password(self, token: str, new_password: str) -> None:
        if not token or len(token) > 256:
            raise HTTPException(400, "Invalid or expired reset token")
        reset_hash = token_hash(token)
        user_id = await self.db.scalar(
            select(PasswordResetToken.user_id).where(
                PasswordResetToken.token_hash == reset_hash
            )
        )
        if user_id is None:
            raise HTTPException(400, "Invalid or expired reset token")
        # Lock order is always user -> reset token -> sessions. This matches
        # issuance/change paths and prevents a request/reset deadlock.
        user = await self.db.scalar(
            select(User)
            .where(User.id == user_id, User.is_active.is_(True))
            .with_for_update()
        )
        if user is None:
            raise HTTPException(400, "Invalid or expired reset token")
        now = datetime.now(UTC)
        reset = await self.db.scalar(
            select(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == reset_hash,
                PasswordResetToken.user_id == user.id,
            )
            .with_for_update()
        )
        if (
            reset is None
            or reset.used_at is not None
            or reset.revoked_at is not None
            or reset.expires_at <= now
        ):
            raise HTTPException(400, "Invalid or expired reset token")
        same_password = await to_thread.run_sync(
            verify_password,
            new_password,
            user.password_hash,
        )
        if same_password:
            raise HTTPException(400, "New password must be different")
        user.password_hash = await to_thread.run_sync(
            hash_password,
            new_password,
        )
        reset.used_at = now
        await self._revoke_password_reset_tokens(
            user.id,
            keep_token_id=reset.id,
        )
        await self._revoke_sessions(user.id)
        await self.db.commit()

    async def login(self, email: str, password: str) -> tuple[User, AuthSession, str]:
        user = await self.db.scalar(select(User).where(User.email == email))
        valid = await to_thread.run_sync(verify_password, password, user.password_hash if user else None)
        if not valid or user is None or not user.is_active:
            raise HTTPException(401, "Invalid email or password")
        session, refresh = self._new_session(user.id)
        await self._select_default(user, session)
        self.db.add(session)
        await self.db.commit()
        return user, session, refresh

    async def _signup_replay(
        self,
        payload: SignupRequest,
        idempotency_key: UUID,
    ) -> tuple[User, AuthSession, str] | None:
        business = await self.db.get(Business, idempotency_key)
        if business is None:
            return None
        membership = await self.db.scalar(
            select(BusinessUserMembership).where(
                BusinessUserMembership.business_id == business.id,
                BusinessUserMembership.role == "owner",
            )
        )
        user = await self.db.get(User, membership.user_id) if membership else None
        valid_password = await to_thread.run_sync(
            verify_password,
            payload.password.get_secret_value(),
            user.password_hash if user else None,
        )
        if (
            user is None
            or business.name != payload.business_name
            or user.email != payload.email
            or not valid_password
        ):
            raise HTTPException(400, "Idempotency key already used for another request")
        session, refresh = self._new_session(user.id, business.id)
        self.db.add(session)
        await self.db.commit()
        return user, session, refresh

    async def signup(
        self,
        payload: SignupRequest,
        idempotency_key: UUID | None = None,
    ) -> tuple[User, AuthSession, str]:
        if idempotency_key is not None:
            replay = await self._signup_replay(payload, idempotency_key)
            if replay is not None:
                return replay

        existing = await self.db.scalar(select(User).where(User.email == payload.email))
        if existing is not None:
            raise HTTPException(409, "Email already registered")

        password_hash = await to_thread.run_sync(
            hash_password,
            payload.password.get_secret_value(),
        )
        business = Business(
            id=idempotency_key or uuid4(),
            name=payload.business_name,
            timezone="America/Sao_Paulo",
            active=True,
        )
        user = User(
            id=uuid4(),
            email=payload.email,
            password_hash=password_hash,
            is_active=True,
            platform_role=None,
        )
        access = BusinessAccess(
            business_id=business.id,
            access_mode="free",
            has_had_operational_access=False,
        )
        membership = BusinessUserMembership(
            user_id=user.id,
            business_id=business.id,
            role="owner",
        )
        session, refresh = self._new_session(user.id, business.id)

        try:
            self.db.add_all([business, user])
            await self.db.flush()
            self.db.add_all(
                [access, membership, *default_services_for_business(business.id)]
            )
            await self.db.flush()
            self.db.add(session)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            if idempotency_key is not None:
                replay = await self._signup_replay(payload, idempotency_key)
                if replay is not None:
                    return replay
            existing = await self.db.scalar(select(User).where(User.email == payload.email))
            if existing is not None:
                raise HTTPException(409, "Email already registered") from None
            raise HTTPException(409, "Account could not be created") from None

        return user, session, refresh

    async def refresh(self, token: str | None) -> tuple[User, AuthSession, str]:
        if not token or len(token) > 256:
            raise unauthorized()
        session = await self.db.scalar(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == token_hash(token))
            .with_for_update()
        )
        if session is None or session.revoked_at or session.expires_at <= datetime.now(UTC):
            raise unauthorized()
        user = await self.db.get(User, session.user_id)
        if user is None or not user.is_active:
            raise unauthorized()
        replacement = new_refresh_token()
        session.refresh_token_hash = token_hash(replacement)
        await self._select_default(user, session)
        await self.db.commit()
        return user, session, replacement

    async def authenticate(self, user_id: UUID, session_id: UUID) -> Principal:
        session = await self.db.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.user_id == user_id)
            .with_for_update()
        )
        if session is None or session.revoked_at or session.expires_at <= datetime.now(UTC):
            raise unauthorized()
        user = await self.db.get(User, user_id)
        if user is None or not user.is_active:
            raise unauthorized()
        return Principal(user, session, await self.memberships(user))

    async def logout(self, token: str | None) -> None:
        if token and len(token) <= 256:
            session = await self.db.scalar(
                select(AuthSession)
                .where(AuthSession.refresh_token_hash == token_hash(token))
                .with_for_update()
            )
            if session:
                await WebPushRepository(self.db).remove_for_auth_session(
                    session.id
                )
                session.revoked_at = datetime.now(UTC)
                await self.db.commit()

    async def select_business(self, principal: Principal, business_id: UUID) -> MeResponse:
        if principal.user.platform_role == "super_admin" or business_id not in {
            m.business_id for m in principal.memberships
        }:
            raise HTTPException(403, "Business access denied")
        principal.session.active_business_id = business_id
        await self.db.commit()
        return principal.view()

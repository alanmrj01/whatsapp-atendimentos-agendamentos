from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import MeResponse, MembershipResponse, MembershipRole, SignupRequest
from app.auth.security import (
    REFRESH_TTL_SECONDS,
    hash_password,
    new_refresh_token,
    token_hash,
    verify_password,
)
from app.models import AuthSession, Business, BusinessAccess, BusinessUserMembership, User


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


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def memberships(self, user: User) -> list[MembershipResponse]:
        if user.platform_role == "super_admin":
            return []
        rows = await self.db.execute(
            select(
                BusinessUserMembership,
                Business.name,
                func.coalesce(BusinessAccess.access_mode, "paid"),
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
            )
            for m, name, access_mode in rows
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
        access = BusinessAccess(business_id=business.id, access_mode="free")
        membership = BusinessUserMembership(
            user_id=user.id,
            business_id=business.id,
            role="owner",
        )
        session, refresh = self._new_session(user.id, business.id)

        try:
            self.db.add_all([business, user])
            await self.db.flush()
            self.db.add_all([access, membership])
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

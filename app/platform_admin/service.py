from __future__ import annotations

from collections import defaultdict
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.models import Business, BusinessUserMembership, BusinessWhatsAppConnection, User
from app.platform_admin.schemas import (
    PlatformBusinessCreateRequest,
    PlatformBusinessListResponse,
    PlatformBusinessResponse,
    PlatformBusinessStatusResponse,
)


class PlatformAdminService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_businesses(self) -> PlatformBusinessListResponse:
        businesses = list((await self.db.scalars(
            select(Business).order_by(Business.name, Business.id)
        )).all())
        if not businesses:
            return PlatformBusinessListResponse(businesses=[])

        ids = [business.id for business in businesses]
        owner_rows = await self.db.execute(
            select(BusinessUserMembership.business_id, User.email)
            .join(User, User.id == BusinessUserMembership.user_id)
            .where(
                BusinessUserMembership.business_id.in_(ids),
                BusinessUserMembership.role == "owner",
            )
            .order_by(User.email)
        )
        owners: dict[UUID, list[str]] = defaultdict(list)
        for business_id, email in owner_rows:
            owners[business_id].append(email)

        connection_rows = await self.db.execute(
            select(
                BusinessWhatsAppConnection.business_id,
                BusinessWhatsAppConnection.status,
                BusinessWhatsAppConnection.created_at,
            )
            .where(BusinessWhatsAppConnection.business_id.in_(ids))
            .order_by(BusinessWhatsAppConnection.created_at.desc())
        )
        statuses: dict[UUID, str] = {}
        for business_id, status, _ in connection_rows:
            statuses.setdefault(business_id, status)

        return PlatformBusinessListResponse(businesses=[
            PlatformBusinessResponse(
                id=business.id,
                name=business.name,
                timezone=business.timezone,
                active=business.active,
                owners=owners.get(business.id, []),
                whatsapp_status=statuses.get(business.id, "disconnected"),
            )
            for business in businesses
        ])

    async def create_business(
        self, payload: PlatformBusinessCreateRequest
    ) -> PlatformBusinessResponse:
        existing = await self.db.scalar(select(User).where(User.email == payload.owner_email))
        if existing is not None:
            raise HTTPException(409, "Owner email already registered")

        password_hash = await to_thread.run_sync(
            hash_password, payload.owner_password.get_secret_value()
        )
        business = Business(
            id=uuid4(),
            name=payload.name,
            timezone=payload.timezone,
            active=True,
        )
        owner = User(
            id=uuid4(),
            email=payload.owner_email,
            password_hash=password_hash,
            is_active=True,
            platform_role=None,
        )
        membership = BusinessUserMembership(
            user_id=owner.id,
            business_id=business.id,
            role="owner",
        )
        self.db.add_all([business, owner, membership])
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise HTTPException(409, "Business or owner already exists") from None

        return PlatformBusinessResponse(
            id=business.id,
            name=business.name,
            timezone=business.timezone,
            active=True,
            owners=[owner.email],
            whatsapp_status="disconnected",
        )

    async def set_business_active(
        self, business_id: UUID, active: bool
    ) -> PlatformBusinessStatusResponse:
        business = await self.db.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Business not found")
        business.active = active
        await self.db.commit()
        return PlatformBusinessStatusResponse(id=business.id, active=business.active)

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_super_admin
from app.auth.service import Principal
from app.core.database import get_db
from app.platform_admin.schemas import (
    PlatformBusinessCreateRequest,
    PlatformBusinessListResponse,
    PlatformBusinessResponse,
    PlatformBusinessStatusRequest,
    PlatformBusinessStatusResponse,
)
from app.platform_admin.service import PlatformAdminService

router = APIRouter(prefix="/api/v1/admin", tags=["platform-admin"])
Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[Principal, Depends(require_super_admin)]


@router.get("/businesses", response_model=PlatformBusinessListResponse)
async def list_businesses(_: Admin, db: Db) -> PlatformBusinessListResponse:
    return await PlatformAdminService(db).list_businesses()


@router.post(
    "/businesses",
    response_model=PlatformBusinessResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_business(
    payload: PlatformBusinessCreateRequest,
    _: Admin,
    db: Db,
    idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
) -> PlatformBusinessResponse:
    return await PlatformAdminService(db).create_business(payload, idempotency_key)


@router.patch(
    "/businesses/{business_id}/active",
    response_model=PlatformBusinessStatusResponse,
    dependencies=[Depends(require_origin)],
)
async def set_business_active(
    business_id: UUID,
    payload: PlatformBusinessStatusRequest,
    _: Admin,
    db: Db,
) -> PlatformBusinessStatusResponse:
    return await PlatformAdminService(db).set_business_active(business_id, payload.active)

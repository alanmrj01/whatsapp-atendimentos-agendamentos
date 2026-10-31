from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_super_admin
from app.auth.service import Principal
from app.core.database import get_db
from app.platform_admin.schemas import (
    PlatformBusinessAccessRequest,
    PlatformBusinessAccessResponse,
    PlatformBusinessCreateRequest,
    PlatformBusinessListResponse,
    PlatformBusinessResponse,
    PlatformBusinessStatusRequest,
    PlatformBusinessStatusResponse,
    PlatformAnalyticsSummaryResponse,
)
from app.platform_admin.analytics import PlatformAnalyticsService
from app.platform_admin.service import PlatformAdminService

router = APIRouter(prefix="/api/v1/admin", tags=["platform-admin"])
Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[Principal, Depends(require_super_admin)]


@router.get("/businesses", response_model=PlatformBusinessListResponse)
async def list_businesses(_: Admin, db: Db) -> PlatformBusinessListResponse:
    return await PlatformAdminService(db).list_businesses()


@router.get("/analytics/summary", response_model=PlatformAnalyticsSummaryResponse)
async def analytics_summary(
    _: Admin,
    db: Db,
    days: int = Query(default=30, ge=1, le=90),
) -> PlatformAnalyticsSummaryResponse:
    # Keep the first dashboard deliberately bounded. The UI exposes the
    # supported presets, while the API remains safe for direct admin use.
    normalized_days = days if days in {1, 7, 30, 90} else 30
    return await PlatformAnalyticsService(db).summary(normalized_days)


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


@router.patch(
    "/businesses/{business_id}/access",
    response_model=PlatformBusinessAccessResponse,
    dependencies=[Depends(require_origin)],
)
async def set_business_access(
    business_id: UUID,
    payload: PlatformBusinessAccessRequest,
    _: Admin,
    db: Db,
) -> PlatformBusinessAccessResponse:
    return await PlatformAdminService(db).set_business_access(
        business_id, payload.access_mode
    )

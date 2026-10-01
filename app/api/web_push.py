from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_principal
from app.auth.service import Principal
from app.core.config import (
    Settings,
    WebPushConfigurationError,
    get_settings,
)
from app.core.database import get_db
from app.push.schemas import (
    PushSubscriptionRequest,
    WebPushConfigResponse,
    WebPushSubscriptionResponse,
)
from app.repositories.web_push import WebPushRepository

router = APIRouter(prefix="/api/v1/push", tags=["pwa-web-push"])
Db = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[Principal, Depends(require_principal)]
Config = Annotated[Settings, Depends(get_settings)]


def _operational_business(principal: Principal):
    membership = principal.active_membership()
    if (
        membership.access_mode != "paid"
        and not membership.has_had_operational_access
    ):
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Subscription required",
        )
    return membership


@router.get("/config", response_model=WebPushConfigResponse)
async def web_push_config(
    principal: Identity,
    settings: Config,
) -> WebPushConfigResponse:
    _operational_business(principal)
    if not settings.web_push_enabled:
        return WebPushConfigResponse(enabled=False)
    try:
        configuration = settings.require_web_push_configuration()
    except WebPushConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Web Push unavailable",
        ) from None
    return WebPushConfigResponse(
        enabled=True,
        public_key=configuration.public_key,
    )


@router.post(
    "/subscriptions",
    response_model=WebPushSubscriptionResponse,
    dependencies=[Depends(require_origin)],
)
async def register_web_push_subscription(
    payload: PushSubscriptionRequest,
    principal: Identity,
    settings: Config,
    db: Db,
) -> WebPushSubscriptionResponse:
    membership = _operational_business(principal)
    try:
        settings.require_web_push_configuration()
    except WebPushConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Web Push unavailable",
        ) from None
    await WebPushRepository(db).register(
        user_id=principal.user.id,
        auth_session_id=principal.session.id,
        business_id=membership.business_id,
        payload=payload,
    )
    await db.commit()
    return WebPushSubscriptionResponse(subscribed=True)


@router.delete(
    "/subscriptions",
    response_model=WebPushSubscriptionResponse,
    dependencies=[Depends(require_origin)],
)
async def remove_web_push_subscription(
    payload: PushSubscriptionRequest,
    principal: Identity,
    db: Db,
) -> WebPushSubscriptionResponse:
    membership = _operational_business(principal)
    await WebPushRepository(db).remove(
        user_id=principal.user.id,
        auth_session_id=principal.session.id,
        business_id=membership.business_id,
        endpoint=str(payload.endpoint),
    )
    await db.commit()
    return WebPushSubscriptionResponse(subscribed=False)

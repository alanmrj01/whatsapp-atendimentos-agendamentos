from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth_config, require_origin, require_principal
from app.auth.rate_limit import login_rate_limiter
from app.auth.schemas import (AccessResponse, ActiveBusinessRequest, EmptyRequest, LoginRequest,
    MeResponse, MembershipRole, PublicConnectionResponse, PublicPlanRequest)
from app.auth.security import COOKIE_NAME, COOKIE_PATH, access_token
from app.auth.service import AuthService, Principal
from app.core.config import Environment, Settings
from app.core.database import get_db
from app.models import AuthSession, User
from app.schemas.whatsapp_onboarding import WhatsAppOnboardingPlanResponse, onboarding_plan_response
from app.whatsapp.administration import WhatsAppConnectionAdministrationService
from app.whatsapp.onboarding import WhatsAppOnboardingService

router = APIRouter(prefix="/api/v1", tags=["pwa"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(require_auth_config)]
Identity = Annotated[Principal, Depends(require_principal)]


def token_response(response: Response, settings: Settings, user: User, session: AuthSession, refresh: str) -> AccessResponse:
    response.set_cookie(
        COOKIE_NAME, refresh, httponly=True, secure=settings.environment is Environment.production,
        samesite="lax", path=COOKIE_PATH,
        max_age=max(0, int((session.expires_at - datetime.now(UTC)).total_seconds())),
        expires=session.expires_at,
    )
    return AccessResponse(access_token=access_token(user.id, session.id, settings.auth_jwt_secret.get_secret_value()))


@router.post("/auth/login", response_model=AccessResponse, dependencies=[Depends(require_origin)])
async def login(payload: LoginRequest, response: Response, settings: Config, db: Db):
    await login_rate_limiter.acquire(payload.email)
    user, session, refresh = await AuthService(db).login(payload.email, payload.password.get_secret_value())
    await login_rate_limiter.success(payload.email)
    return token_response(response, settings, user, session, refresh)


@router.post("/auth/refresh", response_model=AccessResponse, dependencies=[Depends(require_origin)])
async def refresh(payload: EmptyRequest, request: Request, response: Response, settings: Config, db: Db):
    user, session, replacement = await AuthService(db).refresh(request.cookies.get(COOKIE_NAME))
    return token_response(response, settings, user, session, replacement)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(require_origin)])
async def logout(payload: EmptyRequest, request: Request, settings: Config, db: Db):
    await AuthService(db).logout(request.cookies.get(COOKIE_NAME))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True,
                           secure=settings.environment is Environment.production, samesite="lax")
    return response


@router.get("/me", response_model=MeResponse)
async def me(principal: Identity):
    return principal.view()


@router.post("/auth/active-business", response_model=MeResponse, dependencies=[Depends(require_origin)])
async def active_business(payload: ActiveBusinessRequest, principal: Identity, db: Db):
    return await AuthService(db).select_business(principal, payload.business_id)


@router.get("/whatsapp/connection", response_model=PublicConnectionResponse)
async def whatsapp_connection(principal: Identity, db: Db):
    business = principal.active_membership()
    connection = await WhatsAppConnectionAdministrationService(db).get_connection(business.business_id)
    return PublicConnectionResponse(status=connection.status.value if connection else "disconnected",
                                    mode=connection.mode.value if connection else None)


@router.post("/whatsapp/onboarding/plan", response_model=WhatsAppOnboardingPlanResponse,
             dependencies=[Depends(require_origin)])
async def onboarding_plan(payload: PublicPlanRequest, principal: Identity, db: Db):
    business = principal.active_membership()
    if business.role not in {MembershipRole.OWNER, MembershipRole.ADMIN}:
        raise HTTPException(403, "Read-only access")
    service = WhatsAppOnboardingService(WhatsAppConnectionAdministrationService(db))
    return onboarding_plan_response(service.plan(payload.intent,
        platform_only_impact_confirmed=payload.platform_only_impact_confirmed))

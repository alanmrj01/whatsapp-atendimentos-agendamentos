from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth_config, require_origin, require_principal
from app.auth.rate_limit import login_rate_limiter
from app.auth.schemas import (
    AccessResponse,
    ActiveBusinessRequest,
    EmptyRequest,
    LoginRequest,
    MetaEmbeddedSignupCompleteRequest,
    MetaEmbeddedSignupStartResponse,
    MeResponse,
    MembershipRole,
    PublicConnectionResponse,
    PublicPlanRequest,
    SignupRequest,
)
from app.auth.security import COOKIE_NAME, COOKIE_PATH, access_token
from app.auth.service import AuthService, Principal
from app.core.config import (
    Environment,
    MetaConfigurationError,
    MetaEmbeddedSignupConfiguration,
    Settings,
)
from app.core.database import get_db
from app.models import AuthSession, User
from app.schemas.whatsapp_onboarding import WhatsAppOnboardingPlanResponse, onboarding_plan_response
from app.whatsapp.administration import (
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.credentials import GoogleSecretManagerCredentialStore
from app.whatsapp.embedded_signup import (
    MetaEmbeddedSignupError,
    MetaEmbeddedSignupGateway,
    MetaEmbeddedSignupRejected,
    MetaEmbeddedSignupService,
    MetaEmbeddedSignupUnavailable,
)
from app.whatsapp.onboarding import (
    WhatsAppOnboardingError,
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingService,
)

router = APIRouter(prefix="/api/v1", tags=["pwa"])
logger = logging.getLogger(__name__)
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(require_auth_config)]
Identity = Annotated[Principal, Depends(require_principal)]


def token_response(response: Response, settings: Settings, user: User, session: AuthSession, refresh: str) -> AccessResponse:
    response.set_cookie(
        COOKIE_NAME,
        refresh,
        httponly=True,
        secure=settings.environment is Environment.production,
        samesite="lax",
        path=COOKIE_PATH,
        max_age=max(0, int((session.expires_at - datetime.now(UTC)).total_seconds())),
        expires=session.expires_at,
    )
    return AccessResponse(
        access_token=access_token(
            user.id,
            session.id,
            settings.auth_jwt_secret.get_secret_value(),
        )
    )


@router.post("/auth/login", response_model=AccessResponse, dependencies=[Depends(require_origin)])
async def login(payload: LoginRequest, response: Response, settings: Config, db: Db):
    await login_rate_limiter.acquire(payload.email)
    user, session, refresh = await AuthService(db).login(
        payload.email,
        payload.password.get_secret_value(),
    )
    await login_rate_limiter.success(payload.email)
    return token_response(response, settings, user, session, refresh)


@router.post(
    "/auth/signup",
    response_model=AccessResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def signup(
    payload: SignupRequest,
    response: Response,
    settings: Config,
    db: Db,
    idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    await login_rate_limiter.acquire(payload.email)
    user, session, refresh = await AuthService(db).signup(payload, idempotency_key)
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
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.environment is Environment.production,
        samesite="lax",
    )
    return response


@router.get("/me", response_model=MeResponse)
async def me(principal: Identity):
    return principal.view()


@router.post("/auth/active-business", response_model=MeResponse, dependencies=[Depends(require_origin)])
async def active_business(payload: ActiveBusinessRequest, principal: Identity, db: Db):
    return await AuthService(db).select_business(principal, payload.business_id)


@router.get(
    "/whatsapp/connection",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
)
async def whatsapp_connection(principal: Identity, db: Db):
    business = principal.active_membership()
    connection = await WhatsAppConnectionAdministrationService(db).get_connection(business.business_id)
    return PublicConnectionResponse(
        status=connection.status.value if connection else "disconnected",
        mode=connection.mode.value if connection else None,
        display_phone_number=(
            connection.masked_display_phone_number if connection else None
        ),
    )


@router.post(
    "/whatsapp/onboarding/plan",
    response_model=WhatsAppOnboardingPlanResponse,
    dependencies=[Depends(require_origin)],
)
async def onboarding_plan(payload: PublicPlanRequest, principal: Identity, db: Db):
    business = principal.active_membership()
    if business.role not in {MembershipRole.OWNER, MembershipRole.ADMIN}:
        raise HTTPException(403, "Read-only access")
    if business.access_mode != "paid":
        raise HTTPException(402, "Paid plan required")
    service = WhatsAppOnboardingService(WhatsAppConnectionAdministrationService(db))
    return onboarding_plan_response(
        service.plan(
            payload.intent,
            platform_only_impact_confirmed=payload.platform_only_impact_confirmed,
        )
    )


def _require_paid_whatsapp_administrator(principal: Principal):
    business = principal.active_membership()
    if business.role not in {MembershipRole.OWNER, MembershipRole.ADMIN}:
        raise HTTPException(403, "Read-only access")
    if business.access_mode != "paid":
        raise HTTPException(402, "Paid plan required")
    return business


def _embedded_signup_service(
    session: AsyncSession,
    configuration: MetaEmbeddedSignupConfiguration,
) -> tuple[MetaEmbeddedSignupService, MetaEmbeddedSignupGateway]:
    gateway = MetaEmbeddedSignupGateway(configuration)
    onboarding = WhatsAppOnboardingService(
        WhatsAppConnectionAdministrationService(session)
    )
    return (
        MetaEmbeddedSignupService(
            onboarding,
            gateway,
            GoogleSecretManagerCredentialStore(configuration.gcp_project_id),
            configuration.graph_version,
        ),
        gateway,
    )


def _embedded_signup_configuration(
    settings: Settings,
) -> MetaEmbeddedSignupConfiguration:
    try:
        return settings.require_meta_embedded_signup_configuration()
    except MetaConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Meta Embedded Signup is not configured",
        ) from None


@router.post(
    "/whatsapp/onboarding/embedded-signup/start",
    response_model=MetaEmbeddedSignupStartResponse,
    dependencies=[Depends(require_origin)],
)
async def start_meta_embedded_signup(
    payload: EmptyRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    configuration = _embedded_signup_configuration(settings)
    administration = WhatsAppConnectionAdministrationService(db)
    current = await administration.get_connection(business.business_id)
    if current is not None and current.status.value == "connected":
        raise HTTPException(409, "WhatsApp account is already connected")
    plan = WhatsAppOnboardingService(administration).plan(
        WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS
    )
    if not plan.ready_to_continue or plan.requested_mode.value != "coexistence":
        raise HTTPException(409, "WhatsApp onboarding path is unavailable")
    return MetaEmbeddedSignupStartResponse(
        app_id=configuration.app_id,
        configuration_id=configuration.configuration_id,
        graph_version=configuration.graph_version,
        embedded_signup_version=configuration.embedded_signup_version,
    )


@router.post(
    "/whatsapp/onboarding/embedded-signup/complete",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def complete_meta_embedded_signup(
    payload: MetaEmbeddedSignupCompleteRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    configuration = _embedded_signup_configuration(settings)
    gateway: MetaEmbeddedSignupGateway | None = None
    try:
        current = await WhatsAppConnectionAdministrationService(
            db
        ).get_connection(business.business_id, for_update=True)
        if current is not None and current.status.value == "connected":
            raise HTTPException(
                409, "WhatsApp account is already connected"
            )
        service, gateway = _embedded_signup_service(db, configuration)
        view = await service.complete_coexistence(
            business.business_id,
            payload.authorization_code,
            waba_id_hint=payload.waba_id,
            phone_number_id_hint=payload.phone_number_id,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except MetaEmbeddedSignupRejected as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(400, "Meta authorization could not be validated") from None
    except (
        MetaEmbeddedSignupUnavailable,
        WhatsAppConfigurationError,
    ) as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(503, "Meta onboarding is temporarily unavailable") from None
    except (
        MetaEmbeddedSignupError,
        WhatsAppOnboardingError,
        WhatsAppConnectionAdministrationError,
    ) as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(409, "WhatsApp onboarding could not be completed") from None
    except Exception:
        await db.rollback()
        raise
    finally:
        if gateway is not None:
            await gateway.aclose()
    return PublicConnectionResponse(
        status=view.status.value,
        mode=view.mode.value,
        display_phone_number=view.masked_display_phone_number,
    )


def _log_embedded_signup_rejection(exc: Exception) -> None:
    logger.info(
        "meta_embedded_signup_rejected",
        extra={"error_type": type(exc).__name__},
    )

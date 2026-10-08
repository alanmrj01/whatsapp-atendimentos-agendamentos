from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth_config, require_origin, require_principal
from app.auth.password_email import BrevoPasswordResetMailer, PasswordResetEmailError
from app.auth.rate_limit import login_rate_limiter, password_reset_rate_limiter
from app.auth.schemas import (
    AccessResponse,
    ActiveBusinessRequest,
    EmptyRequest,
    LoginRequest,
    MetaApiOnlyEmbeddedSignupCompleteRequest,
    MetaApiOnlyEmbeddedSignupStartRequest,
    MetaApiOnlyEmbeddedSignupStartResponse,
    MetaEmbeddedSignupAssetsRequest,
    MetaEmbeddedSignupCompleteRequest,
    MetaEmbeddedSignupStartResponse,
    MetaEmbeddedSignupTelemetryRequest,
    MeResponse,
    MembershipRole,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PublicConnectionResponse,
    PublicPlanRequest,
    WhatsAppModePreferenceRequest,
    SignupRequest,
)
from app.auth.security import COOKIE_NAME, COOKIE_PATH, access_token
from app.auth.service import AuthService, Principal
from app.core.config import (
    Environment,
    MetaConfigurationError,
    MetaEmbeddedSignupConfiguration,
    PasswordRecoveryConfigurationError,
    Settings,
)
from app.core.database import get_db
from app.models import AuthSession, Business, User
from app.schemas.whatsapp_onboarding import WhatsAppOnboardingPlanResponse, onboarding_plan_response
from app.whatsapp.administration import (
    META_ONBOARDING_PENDING,
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.connections import WhatsAppConnectionMode
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


def _public_pending_state(error_code: str | None) -> str | None:
    return (
        "authorization_pending"
        if error_code == META_ONBOARDING_PENDING
        else None
    )


def _public_connection(view) -> PublicConnectionResponse:
    return PublicConnectionResponse(
        status=view.status.value,
        mode=view.mode.value,
        display_phone_number=view.masked_display_phone_number,
        pending_state=_public_pending_state(view.last_error_code),
        review_status=view.meta_review_status,
        preferred_mode=(
            view.preferred_mode.value
            if view.preferred_mode is not None
            else None
        ),
        mode_switch_requested_at=view.mode_switch_requested_at,
        mode_switch_last_checked_at=view.mode_switch_last_checked_at,
        mode_switch_next_check_at=view.mode_switch_next_check_at,
    )


def _require_password_recovery(settings: Settings) -> None:
    try:
        settings.require_password_recovery_enabled()
    except PasswordRecoveryConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Password recovery is temporarily unavailable",
        ) from None


def _password_reset_mailer(settings: Settings) -> BrevoPasswordResetMailer:
    try:
        configuration = settings.require_password_reset_email_configuration()
    except PasswordRecoveryConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Password recovery is temporarily unavailable",
        ) from None
    return BrevoPasswordResetMailer(configuration)


async def token_response(
    response: Response,
    settings: Settings,
    user: User,
    session: AuthSession,
    refresh: str,
    db: AsyncSession,
) -> AccessResponse:
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
    memberships = await AuthService(db).memberships(user)
    return AccessResponse(
        access_token=access_token(
            user.id,
            session.id,
            settings.auth_jwt_secret.get_secret_value(),
        ),
        session=Principal(user=user, session=session, memberships=memberships).view(),
    )


@router.post("/auth/login", response_model=AccessResponse, dependencies=[Depends(require_origin)])
async def login(payload: LoginRequest, response: Response, settings: Config, db: Db):
    await login_rate_limiter.acquire(payload.email)
    user, session, refresh = await AuthService(db).login(
        payload.email,
        payload.password.get_secret_value(),
    )
    await login_rate_limiter.success(payload.email)
    return await token_response(response, settings, user, session, refresh, db)


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
    return await token_response(response, settings, user, session, refresh, db)


@router.post("/auth/refresh", response_model=AccessResponse, dependencies=[Depends(require_origin)])
async def refresh(payload: EmptyRequest, request: Request, response: Response, settings: Config, db: Db):
    user, session, replacement = await AuthService(db).refresh(request.cookies.get(COOKIE_NAME))
    return await token_response(response, settings, user, session, replacement, db)


@router.post(
    "/auth/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_origin)],
)
async def forgot_password(
    payload: PasswordResetRequest,
    settings: Config,
    db: Db,
):
    mailer = _password_reset_mailer(settings)
    await password_reset_rate_limiter.acquire(payload.email)
    service = AuthService(db)
    issue = await service.issue_password_reset(payload.email)
    if issue is not None:
        try:
            await mailer.send(
                reset_id=issue.id,
                email=issue.email,
                token=issue.token,
            )
        except PasswordResetEmailError:
            await service.revoke_password_reset(issue.id)
            logger.error("password_reset_delivery_failed")
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/auth/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def reset_password(
    payload: PasswordResetConfirmRequest,
    settings: Config,
    db: Db,
):
    _require_password_recovery(settings)
    await AuthService(db).reset_password(
        payload.token.get_secret_value(),
        payload.new_password.get_secret_value(),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/auth/password/change",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def change_password(
    payload: PasswordChangeRequest,
    settings: Config,
    principal: Identity,
    db: Db,
):
    _require_password_recovery(settings)
    await AuthService(db).change_password(
        principal,
        payload.current_password.get_secret_value(),
        payload.new_password.get_secret_value(),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    if connection is None:
        return PublicConnectionResponse(status="disconnected")
    return _public_connection(connection)


@router.post(
    "/whatsapp/mode-preference",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def set_whatsapp_mode_preference(
    payload: WhatsAppModePreferenceRequest,
    principal: Identity,
    db: Db,
):
    membership = _require_paid_whatsapp_administrator(principal)
    administration = WhatsAppConnectionAdministrationService(db)
    if payload.preferred_mode is None:
        view = await administration.clear_mode_switch_request(
            membership.business_id
        )
    else:
        view = await administration.request_mode_switch(
            membership.business_id,
            WhatsAppConnectionMode(payload.preferred_mode),
        )
    await db.commit()
    return _public_connection(view)


@router.post(
    "/whatsapp/disconnect",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def disconnect_whatsapp(
    principal: Identity,
    db: Db,
):
    membership = _require_paid_whatsapp_administrator(principal)
    administration = WhatsAppConnectionAdministrationService(db)
    current = await administration.get_connection(
        membership.business_id,
        for_update=True,
    )
    view = None
    if current is not None and current.status.value != "disconnected":
        view = await administration.mark_disconnected(membership.business_id)

    # Always clear legacy/pilot markers too. A tenant may have migrated from the
    # legacy fields to the versioned connection table and must not remain
    # logically connected after the versioned record is disconnected.
    business = await db.get(Business, membership.business_id)
    if business is not None:
        business.meta_phone_number_id = None
        business.meta_waba_id = None

    await db.commit()
    return PublicConnectionResponse(
        status="disconnected",
        mode=view.mode.value if view is not None else None,
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


def _require_api_only_fallback(settings: Settings) -> None:
    if not settings.whatsapp_api_only_fallback_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WhatsApp exclusive connection is temporarily unavailable",
        )


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
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "coexistence"
    ):
        raise HTTPException(409, "WhatsApp account is already connected in this mode")
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "api_only"
    ):
        await administration.request_mode_switch(
            business.business_id,
            WhatsAppConnectionMode.COEXISTENCE,
        )
        await db.commit()
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
    "/whatsapp/onboarding/embedded-signup/attempt",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def begin_meta_embedded_signup_attempt(
    payload: EmptyRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    _embedded_signup_configuration(settings)
    administration = WhatsAppConnectionAdministrationService(db)
    current = await administration.get_connection(
        business.business_id,
        for_update=True,
    )
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "coexistence"
    ):
        raise HTTPException(409, "WhatsApp account is already connected in this mode")
    plan = WhatsAppOnboardingService(administration).plan(
        WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS
    )
    if not plan.ready_to_continue or plan.requested_mode.value != "coexistence":
        raise HTTPException(409, "WhatsApp onboarding path is unavailable")
    try:
        if current is not None and current.status.value == "connected":
            # During a mode switch the existing connection remains operational
            # until Meta confirms the replacement. No pending row is created.
            view = current
        else:
            view = await administration.begin_pending_connection(
                business.business_id,
                plan.requested_mode,
            )
            await db.commit()
    except WhatsAppConnectionAdministrationError:
        await db.rollback()
        raise HTTPException(
            409, "WhatsApp onboarding could not be started"
        ) from None
    return _public_connection(view)


@router.post(
    "/whatsapp/onboarding/embedded-signup/assets",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def record_meta_embedded_signup_assets(
    payload: MetaEmbeddedSignupAssetsRequest,
    principal: Identity,
    settings: Config,
    db: Db,
) -> Response:
    business = _require_paid_whatsapp_administrator(principal)
    configuration = _embedded_signup_configuration(settings)
    administration = WhatsAppConnectionAdministrationService(db)
    try:
        await administration.record_pending_meta_assets(
            business.business_id,
            meta_waba_id=payload.waba_id,
            meta_phone_number_id=payload.phone_number_id,
            graph_version=configuration.graph_version,
        )
        await db.commit()
    except WhatsAppConnectionAdministrationError:
        await db.rollback()
        raise HTTPException(
            409, "WhatsApp onboarding progress could not be saved"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/whatsapp/onboarding/embedded-signup/telemetry",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def meta_embedded_signup_telemetry(
    payload: MetaEmbeddedSignupTelemetryRequest,
    principal: Identity,
) -> Response:
    _require_paid_whatsapp_administrator(principal)
    fields = {
        name: value
        for name, value in payload.model_dump(exclude={"stage"}).items()
        if value is not None
    }
    logger.info(
        "meta_embedded_signup_client_progress",
        extra={"stage": payload.stage, **fields},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    completed = False
    logger.info(
        "meta_embedded_signup_progress",
        extra={
            "stage": "complete_request_started",
            "waba_id_received": bool(payload.waba_id),
            "phone_number_id_received": payload.phone_number_id is not None,
        },
    )
    try:
        current = await WhatsAppConnectionAdministrationService(
            db
        ).get_connection(business.business_id, for_update=True)
        if (
            current is not None
            and current.status.value == "connected"
            and current.mode.value == "coexistence"
        ):
            raise HTTPException(
                409, "WhatsApp account is already connected in this mode"
            )
        service, gateway = _embedded_signup_service(db, configuration)
        view = await service.complete_coexistence(
            business.business_id,
            payload.authorization_code,
            waba_id_hint=payload.waba_id,
            phone_number_id_hint=payload.phone_number_id,
        )
        await db.commit()
        completed = True
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
        _log_embedded_signup_stage(
            "complete_request_succeeded" if completed else "complete_request_failed"
        )
    return _public_connection(view)


@router.post(
    "/whatsapp/onboarding/api-only/start",
    response_model=MetaApiOnlyEmbeddedSignupStartResponse,
    dependencies=[Depends(require_origin)],
)
async def start_meta_api_only_signup(
    payload: MetaApiOnlyEmbeddedSignupStartRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    _require_api_only_fallback(settings)
    configuration = _embedded_signup_configuration(settings)
    if configuration.embedded_signup_version != "v4":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WhatsApp exclusive connection requires Embedded Signup v4",
        )
    administration = WhatsAppConnectionAdministrationService(db)
    current = await administration.get_connection(business.business_id)
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "api_only"
    ):
        raise HTTPException(409, "WhatsApp account is already connected in this mode")
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "coexistence"
    ):
        await administration.request_mode_switch(
            business.business_id,
            WhatsAppConnectionMode.API_ONLY,
        )
        await db.commit()
    plan = WhatsAppOnboardingService(administration).plan(
        payload.intent,
        platform_only_impact_confirmed=payload.platform_only_impact_confirmed,
    )
    if not plan.ready_to_continue or plan.requested_mode.value != "api_only":
        raise HTTPException(
            409,
            "Explicit confirmation is required before exclusive connection",
        )
    return MetaApiOnlyEmbeddedSignupStartResponse(
        app_id=configuration.app_id,
        configuration_id=configuration.configuration_id,
        graph_version=configuration.graph_version,
        embedded_signup_version=configuration.embedded_signup_version,
        intent=payload.intent,
    )


@router.post(
    "/whatsapp/onboarding/api-only/complete",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def complete_meta_api_only_signup(
    payload: MetaApiOnlyEmbeddedSignupCompleteRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    _require_api_only_fallback(settings)
    configuration = _embedded_signup_configuration(settings)
    administration = WhatsAppConnectionAdministrationService(db)
    current = await administration.get_connection(
        business.business_id, for_update=True
    )
    if (
        current is not None
        and current.status.value == "connected"
        and current.mode.value == "api_only"
    ):
        raise HTTPException(409, "WhatsApp account is already connected in this mode")
    plan = WhatsAppOnboardingService(administration).plan(
        payload.intent,
        platform_only_impact_confirmed=payload.platform_only_impact_confirmed,
    )
    if not plan.ready_to_continue or plan.requested_mode.value != "api_only":
        raise HTTPException(
            409,
            "Explicit confirmation is required before exclusive connection",
        )

    gateway: MetaEmbeddedSignupGateway | None = None
    completed = False
    logger.info(
        "meta_api_only_signup_progress",
        extra={
            "stage": "complete_request_started",
            "intent": payload.intent.value,
            "waba_id_received": bool(payload.waba_id),
            "phone_number_id_received": payload.phone_number_id is not None,
        },
    )
    try:
        service, gateway = _embedded_signup_service(db, configuration)
        view = await service.complete_api_only(
            business.business_id,
            payload.authorization_code,
            intent=payload.intent,
            platform_only_impact_confirmed=payload.platform_only_impact_confirmed,
            registration_pin=payload.registration_pin,
            waba_id_hint=payload.waba_id,
            phone_number_id_hint=payload.phone_number_id,
        )
        await db.commit()
        completed = True
    except HTTPException:
        await db.rollback()
        raise
    except MetaEmbeddedSignupRejected as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            400, "Meta exclusive connection could not be validated"
        ) from None
    except (
        MetaEmbeddedSignupUnavailable,
        WhatsAppConfigurationError,
    ) as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            503, "Meta onboarding is temporarily unavailable"
        ) from None
    except (
        MetaEmbeddedSignupError,
        WhatsAppOnboardingError,
        WhatsAppConnectionAdministrationError,
    ) as exc:
        await db.rollback()
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            409, "WhatsApp exclusive connection could not be completed"
        ) from None
    except Exception:
        await db.rollback()
        raise
    finally:
        if gateway is not None:
            await gateway.aclose()
        logger.info(
            "meta_api_only_signup_progress",
            extra={
                "stage": (
                    "complete_request_succeeded"
                    if completed
                    else "complete_request_failed"
                )
            },
        )
    return _public_connection(view)


def _log_embedded_signup_rejection(exc: Exception) -> None:
    logger.info(
        "meta_embedded_signup_rejected",
        extra={"error_type": type(exc).__name__},
    )


def _log_embedded_signup_stage(stage: str) -> None:
    logger.info(
        "meta_embedded_signup_progress",
        extra={"stage": stage},
    )

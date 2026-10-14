from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit
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
    MetaEmbeddedSignupAttemptRequest,
    MetaEmbeddedSignupCompleteRequest,
    MetaEmbeddedSignupRedirectCancelRequest,
    MetaEmbeddedSignupRedirectStartRequest,
    MetaEmbeddedSignupRedirectStartResponse,
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
    WhatsAppPrepareCoexistenceRequest,
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
    META_AUTHORIZATION_INCOMPLETE,
    META_ONBOARDING_EXPIRED,
    META_ONBOARDING_FAILED,
    META_ONBOARDING_PENDING,
    META_PROVIDER_REJECTED,
    META_PROVIDER_UNAVAILABLE,
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.connections import WhatsAppConnectionMode
from app.whatsapp.credentials import (
    BusinessWhatsAppCredentialProvider,
    GoogleSecretManagerCredentialStore,
)
from app.whatsapp.embedded_signup import (
    MetaEmbeddedSignupError,
    MetaEmbeddedSignupGateway,
    MetaEmbeddedSignupRejected,
    MetaEmbeddedSignupService,
    MetaEmbeddedSignupUnavailable,
    meta_onboarding_log_context,
)
from app.whatsapp.embedded_signup_redirect import (
    MetaEmbeddedSignupRedirectError,
    MetaEmbeddedSignupRedirectState,
    build_meta_embedded_signup_redirect,
    decode_meta_embedded_signup_redirect_state,
)
from app.repositories.whatsapp_connections import (
    WhatsAppConnectionRepository,
    connection_record,
)
from app.whatsapp.onboarding import (
    WhatsAppOnboardingError,
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingService,
)
from app.whatsapp.onboarding_attempts import (
    WhatsAppOnboardingAttemptError,
    WhatsAppOnboardingAttemptService,
    WhatsAppOnboardingAttemptView,
)

router = APIRouter(prefix="/api/v1", tags=["pwa"])
logger = logging.getLogger(__name__)
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(require_auth_config)]
Identity = Annotated[Principal, Depends(require_principal)]


def _public_pending_state(
    view,
    attempt: WhatsAppOnboardingAttemptView | None = None,
) -> str | None:
    if getattr(view, "last_error_code", None) != META_ONBOARDING_PENDING:
        return None
    status_value = getattr(getattr(view, "status", None), "value", None)
    mode_value = getattr(getattr(view, "mode", None), "value", None)
    if status_value != "pending":
        return None

    review_status = getattr(view, "meta_review_status", None)
    if review_status == "rejected":
        return "meta_review_rejected"

    if attempt is not None:
        # A persisted attempt is authoritative. Receiving WABA/phone assets
        # during Embedded Signup does not itself mean Meta's account review is
        # blocking the technical connection.
        return "authorization_pending"

    if (
        mode_value == "coexistence"
        and getattr(view, "has_phone_number_id", False)
        and review_status is None
    ):
        # Backward compatibility for pending rows created before attempt
        # orchestration existed. New attempts never infer review from assets.
        return "meta_review_pending"
    return "authorization_pending"


def _public_connection_journey(
    view,
    pending_state: str | None,
    attempt: WhatsAppOnboardingAttemptView | None = None,
) -> tuple[str, bool, str]:
    status_value = getattr(getattr(view, "status", None), "value", None)
    if status_value == "connected":
        return ("connected", False, "none")
    if status_value == "disconnected":
        return ("not_started", True, "choose_mode")
    if status_value == "error":
        return ("error", True, "resolve_connection")
    if pending_state == "meta_review_pending":
        return ("meta_review_pending", False, "wait_for_meta_review")
    if pending_state == "meta_review_rejected":
        return ("meta_review_rejected", True, "review_meta_rejection")
    if attempt is not None and (
        attempt.state in {"code_received", "completing"}
        or (
            attempt.state == "assets_received"
            and attempt.authorization_code_received
        )
    ):
        return ("validating_connection", False, "wait_for_completion")
    return ("authorization_pending", True, "continue_authorization")


_ERROR_REASON_BY_CODE = {
    META_AUTHORIZATION_INCOMPLETE: "authorization_incomplete",
    META_ONBOARDING_EXPIRED: "onboarding_expired",
    META_PROVIDER_REJECTED: "provider_rejected",
    META_PROVIDER_UNAVAILABLE: "provider_unavailable",
    META_ONBOARDING_FAILED: "onboarding_failed",
}


def _public_connection(
    view,
    attempt: WhatsAppOnboardingAttemptView | None = None,
) -> PublicConnectionResponse:
    preferred_mode = getattr(view, "preferred_mode", None)
    pending_state = _public_pending_state(view, attempt)
    journey_state, requires_user_action, next_action = _public_connection_journey(
        view,
        pending_state,
        attempt,
    )
    last_error_code = getattr(view, "last_error_code", None)
    return PublicConnectionResponse(
        status=view.status.value,
        mode=view.mode.value,
        display_phone_number=getattr(
            view, "masked_display_phone_number", None
        ),
        error_reason=_ERROR_REASON_BY_CODE.get(last_error_code),
        onboarding_attempt_id=attempt.id if attempt is not None else None,
        onboarding_attempt_state=attempt.state if attempt is not None else None,
        onboarding_last_stage=attempt.last_stage if attempt is not None else None,
        onboarding_last_event_at=(
            attempt.last_event_at if attempt is not None else None
        ),
        onboarding_expires_at=attempt.expires_at if attempt is not None else None,
        pending_state=pending_state,
        review_status=getattr(view, "meta_review_status", None),
        preferred_mode=(
            preferred_mode.value
            if preferred_mode is not None
            else None
        ),
        mode_switch_requested_at=getattr(
            view, "mode_switch_requested_at", None
        ),
        mode_switch_last_checked_at=getattr(
            view, "mode_switch_last_checked_at", None
        ),
        mode_switch_next_check_at=getattr(
            view, "mode_switch_next_check_at", None
        ),
        journey_state=journey_state,
        requires_user_action=requires_user_action,
        next_action=next_action,
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
    administration = WhatsAppConnectionAdministrationService(db)
    attempts = WhatsAppOnboardingAttemptService(db)

    await attempts.lock_business(business.business_id)
    attempt = await attempts.reconcile(business.business_id)
    if attempt is not None and attempt.state in {
        "expired",
        "authorization_incomplete",
    }:
        try:
            await administration.mark_onboarding_error(
                business.business_id,
                (
                    META_ONBOARDING_EXPIRED
                    if attempt.state == "expired"
                    else META_AUTHORIZATION_INCOMPLETE
                ),
            )
            await db.commit()
        except WhatsAppConnectionAdministrationError:
            await db.rollback()

    connection = await administration.get_connection(business.business_id)
    if connection is None:
        return PublicConnectionResponse(status="disconnected")
    latest_attempt = await attempts.latest(business.business_id)
    return _public_connection(connection, latest_attempt)


@router.post(
    "/whatsapp/mode-switch/prepare-coexistence",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def prepare_whatsapp_coexistence_switch(
    payload: WhatsAppPrepareCoexistenceRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    membership = _require_paid_whatsapp_administrator(principal)
    if not (
        payload.confirm_temporary_interruption
        and payload.confirm_phone_available
    ):
        raise HTTPException(
            422,
            "Explicit migration confirmations are required",
        )

    repository = WhatsAppConnectionRepository(db)
    current = await repository.get_connection(
        membership.business_id,
        for_update=True,
    )
    if (
        current is None
        or current.status != "connected"
        or current.mode != "api_only"
        or current.meta_phone_number_id is None
    ):
        raise HTTPException(
            409,
            "Exclusive WhatsApp connection is not available for migration",
        )

    administration = WhatsAppConnectionAdministrationService(db)
    await administration.request_mode_switch(
        membership.business_id,
        WhatsAppConnectionMode.COEXISTENCE,
    )

    try:
        access_token = await BusinessWhatsAppCredentialProvider(
            settings
        ).resolve(connection_record(current))
        configuration = _embedded_signup_configuration(settings)
        gateway = MetaEmbeddedSignupGateway(configuration)
        try:
            await gateway.deregister_phone(
                current.meta_phone_number_id,
                access_token,
            )
        finally:
            await gateway.aclose()
    except (WhatsAppConfigurationError, MetaEmbeddedSignupError):
        await db.rollback()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Meta could not prepare the WhatsApp mode change",
        ) from None

    view = await administration.mark_disconnected(
        membership.business_id
    )
    business = await db.get(Business, membership.business_id)
    if business is not None:
        business.meta_phone_number_id = None
        business.meta_waba_id = None
    await db.commit()
    return _public_connection(view)


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


def _verified_meta_redirect_state(
    settings: Settings,
    configuration: MetaEmbeddedSignupConfiguration,
    raw_state: str,
    business_id: UUID,
) -> MetaEmbeddedSignupRedirectState:
    try:
        state = decode_meta_embedded_signup_redirect_state(
            configuration,
            raw_state,
        )
    except MetaEmbeddedSignupRedirectError:
        raise HTTPException(400, "Meta redirect state is invalid or expired") from None
    if state.business_id != business_id:
        raise HTTPException(409, "WhatsApp onboarding attempt is no longer active")
    parsed = urlsplit(state.redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if (
        origin not in settings.allowed_pwa_origins()
        or parsed.path != "/app/whatsapp/meta-return"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(400, "Meta redirect URI is invalid")
    return state


def _require_api_only_fallback(settings: Settings) -> None:
    if not settings.whatsapp_api_only_fallback_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WhatsApp exclusive connection is temporarily unavailable",
        )


async def _persist_embedded_signup_failure(
    db: AsyncSession,
    business_id: UUID,
    attempt_id: UUID | None,
    error_code: str,
) -> None:
    """Persist a recoverable failure without corrupting a newer attempt."""
    attempts = WhatsAppOnboardingAttemptService(db)
    administration = WhatsAppConnectionAdministrationService(db)
    try:
        if attempt_id is not None:
            active = await attempts.active(business_id, for_update=True)
            if active is None or active.id != attempt_id:
                await db.rollback()
                return
            await attempts.mark_failed(
                business_id,
                attempt_id,
                error_code,
            )
        await administration.mark_onboarding_error(
            business_id,
            error_code,
        )
        await db.commit()
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
        await db.rollback()
        logger.exception(
            "meta_embedded_signup_failure_state_persist_failed",
            extra={
                "business_id": str(business_id),
                "attempt_id": str(attempt_id) if attempt_id else None,
                "failure_code": error_code,
            },
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
    if current is not None and current.status.value == "connected":
        detail = (
            "WhatsApp account is already connected in this mode"
            if current.mode.value == "coexistence"
            else "Use the assisted mode-switch flow before coexistence onboarding"
        )
        raise HTTPException(409, detail)
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
    idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
):
    business = _require_paid_whatsapp_administrator(principal)
    _embedded_signup_configuration(settings)
    administration = WhatsAppConnectionAdministrationService(db)
    attempts = WhatsAppOnboardingAttemptService(db)
    try:
        await attempts.lock_business(business.business_id)

        reconciled = await attempts.reconcile(business.business_id)
        if reconciled is not None and reconciled.state in {
            "expired",
            "authorization_incomplete",
        }:
            try:
                await administration.mark_onboarding_error(
                    business.business_id,
                    (
                        META_ONBOARDING_EXPIRED
                        if reconciled.state == "expired"
                        else META_AUTHORIZATION_INCOMPLETE
                    ),
                )
            except WhatsAppConnectionAdministrationError:
                pass

        current = await administration.get_connection(
            business.business_id,
            for_update=True,
        )
        if current is not None and current.status.value == "connected":
            detail = (
                "WhatsApp account is already connected in this mode"
                if current.mode.value == "coexistence"
                else "Use the assisted mode-switch flow before coexistence onboarding"
            )
            raise HTTPException(409, detail)

        active = await attempts.active(
            business.business_id,
            for_update=True,
        )
        if (
            current is not None
            and current.status.value == "pending"
            and active is None
        ):
            # Legacy stranded pending rows predate attempt orchestration.
            # Convert them to a retryable error first so begin_pending_connection
            # clears stale WABA/phone metadata before the new attempt.
            await administration.mark_onboarding_error(
                business.business_id,
                META_ONBOARDING_EXPIRED,
            )

        plan = WhatsAppOnboardingService(administration).plan(
            WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS
        )
        if (
            not plan.ready_to_continue
            or plan.requested_mode.value != "coexistence"
        ):
            raise HTTPException(409, "WhatsApp onboarding path is unavailable")

        view = await administration.begin_pending_connection(
            business.business_id,
            plan.requested_mode,
        )
        attempt = await attempts.begin(
            business.business_id,
            view.id,
            plan.requested_mode,
            request_id=idempotency_key,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
        await db.rollback()
        raise HTTPException(
            409, "WhatsApp onboarding could not be started"
        ) from None

    logger.info(
        "meta_embedded_signup_attempt",
        extra={
            "stage": "attempt_started",
            "business_id": str(business.business_id),
            "attempt_id": str(attempt.id),
        },
    )
    return _public_connection(view, attempt)


@router.post(
    "/whatsapp/onboarding/embedded-signup/redirect/start",
    response_model=MetaEmbeddedSignupRedirectStartResponse,
    dependencies=[Depends(require_origin)],
)
async def start_meta_embedded_signup_redirect(
    payload: MetaEmbeddedSignupRedirectStartRequest,
    request: Request,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    configuration = _embedded_signup_configuration(settings)
    attempts = WhatsAppOnboardingAttemptService(db)
    administration = WhatsAppConnectionAdministrationService(db)
    try:
        await attempts.lock_business(business.business_id)
        active = await attempts.active(
            business.business_id,
            for_update=True,
        )
        current = await administration.get_connection(
            business.business_id,
            for_update=True,
        )
        if (
            active is None
            or active.id != payload.attempt_id
            or current is None
            or current.status.value == "connected"
            or active.connection_id != current.id
        ):
            raise HTTPException(
                409,
                "WhatsApp onboarding attempt is no longer active",
            )
        origin = request.headers.get("origin", "").rstrip("/")
        redirect_uri = f"{origin}/app/whatsapp/meta-return"
        authorization_url, _ = build_meta_embedded_signup_redirect(
            configuration,
            business_id=business.business_id,
            attempt_id=active.id,
            redirect_uri=redirect_uri,
        )
        observed = await attempts.record_event(
            business.business_id,
            active.id,
            stage="login_opened",
        )
        if observed.state == "expired":
            raise HTTPException(
                409,
                "WhatsApp onboarding attempt is no longer active",
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except WhatsAppOnboardingAttemptError:
        await db.rollback()
        raise HTTPException(
            409,
            "WhatsApp onboarding attempt is no longer active",
        ) from None

    logger.info(
        "meta_embedded_signup_progress",
        extra={
            "stage": "redirect_authorization_started",
            "business_id": str(business.business_id),
            "attempt_id": str(payload.attempt_id),
        },
    )
    return MetaEmbeddedSignupRedirectStartResponse(
        attempt_id=payload.attempt_id,
        authorization_url=authorization_url,
    )


@router.post(
    "/whatsapp/onboarding/embedded-signup/incomplete",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def mark_meta_embedded_signup_incomplete(
    payload: MetaEmbeddedSignupAttemptRequest,
    principal: Identity,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    administration = WhatsAppConnectionAdministrationService(db)
    attempts = WhatsAppOnboardingAttemptService(db)
    attempt: WhatsAppOnboardingAttemptView | None = None
    try:
        await attempts.lock_business(business.business_id)
        view = await administration.get_connection(
            business.business_id,
            for_update=True,
        )
        if view is None:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp business connection is not available"
            )

        if payload.attempt_id is not None:
            active = await attempts.active(
                business.business_id,
                for_update=True,
            )
            if active is not None and active.id == payload.attempt_id:
                attempt = active
            else:
                attempt = await attempts.latest(business.business_id)
        else:
            # Backward compatibility for older browser bundles. An empty
            # callback is not authoritative on mobile/custom-tab flows, so a
            # legacy client signal must never downgrade the server lifecycle.
            attempt = await attempts.latest(business.business_id)

        await db.commit()
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
        await db.rollback()
        raise HTTPException(
            409, "WhatsApp onboarding state could not be updated"
        ) from None

    logger.info(
        "meta_embedded_signup_progress",
        extra={
            "stage": "authorization_incomplete_client_signal_ignored",
            "business_id": str(business.business_id),
            "attempt_id": (
                str(payload.attempt_id) if payload.attempt_id else None
            ),
        },
    )
    return _public_connection(view, attempt)


@router.post(
    "/whatsapp/onboarding/embedded-signup/redirect/cancel",
    response_model=PublicConnectionResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_origin)],
)
async def cancel_meta_embedded_signup_redirect(
    payload: MetaEmbeddedSignupRedirectCancelRequest,
    principal: Identity,
    settings: Config,
    db: Db,
):
    business = _require_paid_whatsapp_administrator(principal)
    configuration = _embedded_signup_configuration(settings)
    state = _verified_meta_redirect_state(
        settings,
        configuration,
        payload.state,
        business.business_id,
    )
    attempts = WhatsAppOnboardingAttemptService(db)
    administration = WhatsAppConnectionAdministrationService(db)
    try:
        await attempts.lock_business(business.business_id)
        current = await administration.get_connection(
            business.business_id,
            for_update=True,
        )
        if current is not None and current.status.value == "connected":
            latest = await attempts.latest(business.business_id)
            await db.commit()
            return _public_connection(current, latest)

        active = await attempts.active(
            business.business_id,
            for_update=True,
        )
        if (
            active is None
            or active.id != state.attempt_id
            or current is None
            or active.connection_id != current.id
        ):
            raise HTTPException(
                409,
                "WhatsApp onboarding attempt is no longer active",
            )
        attempt = await attempts.mark_authorization_incomplete(
            business.business_id,
            state.attempt_id,
        )
        view = await administration.mark_onboarding_error(
            business.business_id,
            META_AUTHORIZATION_INCOMPLETE,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
        await db.rollback()
        raise HTTPException(
            409,
            "WhatsApp onboarding attempt is no longer active",
        ) from None

    logger.info(
        "meta_embedded_signup_progress",
        extra={
            "stage": "redirect_authorization_cancelled",
            "business_id": str(business.business_id),
            "attempt_id": str(state.attempt_id),
        },
    )
    return _public_connection(view, attempt)


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
    attempts = WhatsAppOnboardingAttemptService(db)
    try:
        await attempts.lock_business(business.business_id)
        if payload.attempt_id is not None:
            active = await attempts.active(
                business.business_id,
                for_update=True,
            )
            if active is None or active.id != payload.attempt_id:
                await db.rollback()
                logger.info(
                    "meta_embedded_signup_stale_event",
                    extra={
                        "stage": "assets",
                        "business_id": str(business.business_id),
                        "attempt_id": str(payload.attempt_id),
                    },
                )
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            current = await administration.get_connection(
                business.business_id,
                for_update=True,
            )
            if current is None or current.id != active.connection_id:
                await db.rollback()
                return Response(status_code=status.HTTP_204_NO_CONTENT)

        await administration.record_pending_meta_assets(
            business.business_id,
            meta_waba_id=payload.waba_id,
            meta_phone_number_id=payload.phone_number_id,
            graph_version=configuration.graph_version,
        )
        if payload.attempt_id is not None:
            await attempts.record_assets(
                business.business_id,
                payload.attempt_id,
                has_waba_id=True,
                has_phone_number_id=payload.phone_number_id is not None,
            )
        await db.commit()
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
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
    db: Db,
) -> Response:
    business = _require_paid_whatsapp_administrator(principal)
    fields = {
        name: (
            str(value)
            if name == "attempt_id" and value is not None
            else value
        )
        for name, value in payload.model_dump(exclude={"stage"}).items()
        if value is not None
    }
    logger.info(
        "meta_embedded_signup_client_progress",
        extra={
            "stage": payload.stage,
            "business_id": str(business.business_id),
            **fields,
        },
    )

    if payload.attempt_id is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    attempts = WhatsAppOnboardingAttemptService(db)
    administration = WhatsAppConnectionAdministrationService(db)
    try:
        await attempts.lock_business(business.business_id)
        active = await attempts.active(
            business.business_id,
            for_update=True,
        )
        if active is None or active.id != payload.attempt_id:
            await db.rollback()
            logger.info(
                "meta_embedded_signup_stale_event",
                extra={
                    "stage": payload.stage,
                    "business_id": str(business.business_id),
                    "attempt_id": str(payload.attempt_id),
                },
            )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        attempt = await attempts.record_event(
            business.business_id,
            payload.attempt_id,
            stage=payload.stage,
            authorization_code_received=payload.authorization_code_received,
            waba_id_received=payload.waba_id_received,
            phone_number_id_received=payload.phone_number_id_received,
        )
        if attempt.state == "authorization_incomplete":
            await administration.mark_authorization_incomplete(
                business.business_id,
            )
        elif attempt.state == "expired":
            await administration.mark_onboarding_error(
                business.business_id,
                META_ONBOARDING_EXPIRED,
            )
        await db.commit()
    except (
        WhatsAppOnboardingAttemptError,
        WhatsAppConnectionAdministrationError,
    ):
        await db.rollback()
        logger.exception(
            "meta_embedded_signup_telemetry_state_failed",
            extra={
                "stage": payload.stage,
                "business_id": str(business.business_id),
                "attempt_id": str(payload.attempt_id),
            },
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
    attempt_id = payload.attempt_id
    redirect_uri = ""
    if payload.redirect_state is not None:
        state = _verified_meta_redirect_state(
            settings,
            configuration,
            payload.redirect_state,
            business.business_id,
        )
        if attempt_id is not None and attempt_id != state.attempt_id:
            raise HTTPException(
                409,
                "WhatsApp onboarding attempt is no longer active",
            )
        attempt_id = state.attempt_id
        redirect_uri = state.redirect_uri
    administration = WhatsAppConnectionAdministrationService(db)
    attempts = WhatsAppOnboardingAttemptService(db)
    gateway: MetaEmbeddedSignupGateway | None = None
    completed = False
    attempt: WhatsAppOnboardingAttemptView | None = None
    logger.info(
        "meta_embedded_signup_progress",
        extra={
            "stage": "complete_request_started",
            "business_id": str(business.business_id),
            "attempt_id": (
                str(attempt_id) if attempt_id else None
            ),
            "waba_id_received": bool(payload.waba_id),
            "phone_number_id_received": payload.phone_number_id is not None,
        },
    )
    try:
        await attempts.lock_business(business.business_id)
        current = await administration.get_connection(
            business.business_id,
            for_update=True,
        )
        if current is not None and current.status.value == "connected":
            if attempt_id is None:
                raise HTTPException(
                    409,
                    "WhatsApp account is already connected in this mode",
                )
            latest = await attempts.latest(business.business_id)
            if latest is None or latest.id != attempt_id:
                raise HTTPException(
                    409,
                    "WhatsApp onboarding attempt is no longer active",
                )
            completed = True
            return _public_connection(current, latest)

        if attempt_id is not None:
            active = await attempts.active(
                business.business_id,
                for_update=True,
            )
            if (
                active is None
                or active.id != attempt_id
                or current is None
                or active.connection_id != current.id
            ):
                raise HTTPException(
                    409,
                    "WhatsApp onboarding attempt is no longer active",
                )
            attempt = await attempts.mark_completing(
                business.business_id,
                attempt_id,
            )

        service, gateway = _embedded_signup_service(db, configuration)
        with meta_onboarding_log_context(
            business.business_id,
            attempt_id,
        ):
            view = await service.complete_coexistence(
                business.business_id,
                payload.authorization_code,
                waba_id_hint=payload.waba_id,
                phone_number_id_hint=payload.phone_number_id,
                redirect_uri=redirect_uri,
            )
        if attempt_id is not None:
            attempt = await attempts.mark_connected(
                business.business_id,
                attempt_id,
            )
        await db.commit()
        completed = True
    except HTTPException:
        await db.rollback()
        raise
    except MetaEmbeddedSignupRejected as exc:
        await db.rollback()
        await _persist_embedded_signup_failure(
            db,
            business.business_id,
            attempt_id,
            META_PROVIDER_REJECTED,
        )
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            400, "Meta authorization could not be validated"
        ) from None
    except (
        MetaEmbeddedSignupUnavailable,
        WhatsAppConfigurationError,
    ) as exc:
        await db.rollback()
        await _persist_embedded_signup_failure(
            db,
            business.business_id,
            attempt_id,
            META_PROVIDER_UNAVAILABLE,
        )
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            503, "Meta onboarding is temporarily unavailable"
        ) from None
    except (
        MetaEmbeddedSignupError,
        WhatsAppOnboardingError,
        WhatsAppConnectionAdministrationError,
        WhatsAppOnboardingAttemptError,
    ) as exc:
        await db.rollback()
        await _persist_embedded_signup_failure(
            db,
            business.business_id,
            attempt_id,
            META_ONBOARDING_FAILED,
        )
        _log_embedded_signup_rejection(exc)
        raise HTTPException(
            409, "WhatsApp onboarding could not be completed"
        ) from None
    except Exception:
        await db.rollback()
        await _persist_embedded_signup_failure(
            db,
            business.business_id,
            attempt_id,
            META_ONBOARDING_FAILED,
        )
        raise
    finally:
        if gateway is not None:
            await gateway.aclose()
        _log_embedded_signup_stage(
            "complete_request_succeeded"
            if completed
            else "complete_request_failed"
        )

    if attempt is None:
        attempt = await attempts.latest(business.business_id)
    return _public_connection(view, attempt)


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

from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.models import Business
from app.schemas.whatsapp_onboarding import (
    WhatsAppConnectionResponse,
    WhatsAppCurrentConnectionResponse,
    WhatsAppOnboardingCompleteRequest,
    WhatsAppOnboardingPlanRequest,
    WhatsAppOnboardingPlanResponse,
    connection_response,
    onboarding_plan_response,
)
from app.tasks.auth import require_service_oidc_identity
from app.whatsapp.administration import (
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.onboarding import (
    WhatsAppOnboardingError,
    WhatsAppOnboardingService,
    WhatsAppProviderCompletion,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/internal/whatsapp",
    tags=["internal-whatsapp-onboarding"],
)


async def require_whatsapp_onboarding_oidc(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized service request",
        )

    audience = settings.whatsapp_onboarding_oidc_audience
    email = settings.whatsapp_onboarding_invoker_email
    try:
        parsed = urlsplit(audience or "")
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and email is not None
            and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        )
    except ValueError:
        valid = False

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp onboarding authorization unavailable",
        )

    await require_service_oidc_identity(
        audience or "",
        email or "",
        authorization,
    )


async def _require_business(
    session: AsyncSession,
    business_id: uuid.UUID,
) -> None:
    exists = await session.scalar(
        select(Business.id).where(Business.id == business_id)
    )
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business not found",
        )


def _service(session: AsyncSession) -> WhatsAppOnboardingService:
    return WhatsAppOnboardingService(
        WhatsAppConnectionAdministrationService(session)
    )


def _operation_rejected(exc: Exception) -> HTTPException:
    logger.info(
        "whatsapp_onboarding_operation_rejected",
        extra={"error_type": type(exc).__name__},
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="WhatsApp onboarding operation rejected",
    )


@router.get(
    "/connections/{business_id}",
    response_model=WhatsAppCurrentConnectionResponse,
    dependencies=[Depends(require_whatsapp_onboarding_oidc)],
)
async def get_whatsapp_connection(
    business_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WhatsAppCurrentConnectionResponse:
    await _require_business(session, business_id)
    view = await WhatsAppConnectionAdministrationService(
        session
    ).get_connection(business_id)
    if view is None:
        return WhatsAppCurrentConnectionResponse(
            connected=False,
            connection=None,
        )
    return WhatsAppCurrentConnectionResponse(
        connected=view.status.value == "connected",
        connection=connection_response(view),
    )


@router.post(
    "/connections/{business_id}/onboarding/start",
    response_model=WhatsAppOnboardingPlanResponse,
    dependencies=[Depends(require_whatsapp_onboarding_oidc)],
)
async def plan_whatsapp_onboarding(
    business_id: uuid.UUID,
    payload: WhatsAppOnboardingPlanRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WhatsAppOnboardingPlanResponse:
    await _require_business(session, business_id)
    try:
        plan = _service(session).plan(
            payload.intent,
            platform_only_impact_confirmed=(
                payload.platform_only_impact_confirmed
            ),
        )
    except WhatsAppOnboardingError as exc:
        raise _operation_rejected(exc) from None
    return onboarding_plan_response(plan)


@router.post(
    "/connections/{business_id}/onboarding/complete",
    response_model=WhatsAppConnectionResponse,
    dependencies=[Depends(require_whatsapp_onboarding_oidc)],
)
async def complete_whatsapp_onboarding(
    business_id: uuid.UUID,
    payload: WhatsAppOnboardingCompleteRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WhatsAppConnectionResponse:
    try:
        async with session.begin():
            await _require_business(session, business_id)
            view = await _service(session).complete_provider_onboarding(
                business_id,
                WhatsAppProviderCompletion(
                    intent=payload.intent,
                    confirmed_mode=payload.confirmed_mode,
                    meta_waba_id=payload.meta_waba_id,
                    meta_phone_number_id=payload.meta_phone_number_id,
                    graph_version=payload.graph_version,
                    credential_secret_ref=payload.credential_secret_ref,
                    provider_confirmed=payload.provider_confirmed,
                    platform_only_impact_confirmed=(
                        payload.platform_only_impact_confirmed
                    ),
                ),
            )
    except (
        WhatsAppOnboardingError,
        WhatsAppConnectionAdministrationError,
    ) as exc:
        raise _operation_rejected(exc) from None
    return connection_response(view)


@router.post(
    "/connections/{business_id}/disconnect",
    response_model=WhatsAppConnectionResponse,
    dependencies=[Depends(require_whatsapp_onboarding_oidc)],
)
async def disconnect_whatsapp_connection(
    business_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WhatsAppConnectionResponse:
    try:
        async with session.begin():
            await _require_business(session, business_id)
            view = await _service(session).disconnect(business_id)
    except (
        WhatsAppOnboardingError,
        WhatsAppConnectionAdministrationError,
    ) as exc:
        raise _operation_rejected(exc) from None
    return connection_response(view)

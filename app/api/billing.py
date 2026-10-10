from __future__ import annotations

import ipaddress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth_config, require_origin, require_principal
from app.auth.schemas import MembershipRole
from app.auth.service import Principal
from app.billing.asaas import AsaasGateway
from app.billing.schemas import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CheckoutProfileResponse,
    CheckoutStatusResponse,
    CreditCardCheckoutRequest,
    CreditCardCheckoutResponse,
    SubscriptionStatusResponse,
)
from app.billing.service import BillingService
from app.core.config import AsaasConfigurationError, Settings
from app.core.database import get_db

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(require_auth_config)]
Identity = Annotated[Principal, Depends(require_principal)]


def _service(settings: Settings, db: AsyncSession) -> BillingService:
    try:
        configuration = settings.require_asaas_configuration()
    except AsaasConfigurationError:
        raise HTTPException(503, "Billing is temporarily unavailable") from None
    return BillingService(db, AsaasGateway(configuration))


def _billing_business(principal: Principal):
    membership = principal.active_membership()
    if membership.role not in {MembershipRole.OWNER, MembershipRole.ADMIN}:
        raise HTTPException(403, "Billing changes require owner or admin access")
    return membership


def _payer_remote_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
    if request.client and request.client.host:
        candidates.append(request.client.host)
    for candidate in candidates:
        try:
            return ipaddress.ip_address(candidate).compressed
        except ValueError:
            continue
    raise HTTPException(400, "Unable to determine payer IP")


@router.post(
    "/checkouts",
    response_model=CheckoutCreateResponse,
    dependencies=[Depends(require_origin)],
)
async def create_checkout(
    payload: CheckoutCreateRequest,
    principal: Identity,
    settings: Config,
    db: Db,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
):
    membership = _billing_business(principal)
    return await _service(settings, db).create_checkout(
        business_id=membership.business_id,
        payer_email=principal.user.email,
        idempotency_key=idempotency_key,
        payload=payload,
        allowed_origins=settings.allowed_pwa_origins(),
    )


@router.get("/checkout-profile", response_model=CheckoutProfileResponse)
async def checkout_profile(
    principal: Identity,
    settings: Config,
    db: Db,
):
    membership = _billing_business(principal)
    return await _service(settings, db).checkout_profile(
        business_id=membership.business_id,
        payer_email=principal.user.email,
    )


@router.post(
    "/checkouts/{checkout_id}/credit-card",
    response_model=CreditCardCheckoutResponse,
    dependencies=[Depends(require_origin)],
)
async def pay_credit_card(
    checkout_id: UUID,
    payload: CreditCardCheckoutRequest,
    request: Request,
    principal: Identity,
    settings: Config,
    db: Db,
):
    membership = _billing_business(principal)
    return await _service(settings, db).pay_credit_card(
        business_id=membership.business_id,
        payer_email=principal.user.email,
        checkout_id=checkout_id,
        payload=payload,
        remote_ip=_payer_remote_ip(request),
    )


@router.get("/checkouts/{checkout_id}", response_model=CheckoutStatusResponse)
async def checkout_status(
    checkout_id: UUID,
    principal: Identity,
    settings: Config,
    db: Db,
):
    membership = _billing_business(principal)
    return await _service(settings, db).checkout_status(
        business_id=membership.business_id,
        checkout_id=checkout_id,
    )


@router.get("/subscription", response_model=SubscriptionStatusResponse)
async def subscription_status(principal: Identity, settings: Config, db: Db):
    membership = _billing_business(principal)
    return await _service(settings, db).subscription_status(
        business_id=membership.business_id
    )
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_auth_config, require_origin, require_principal
from app.auth.schemas import MembershipRole
from app.auth.service import Principal
from app.billing.asaas import AsaasGateway
from app.billing.schemas import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    CheckoutStatusResponse,
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
        idempotency_key=idempotency_key,
        payload=payload,
        allowed_origins=settings.allowed_pwa_origins(),
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

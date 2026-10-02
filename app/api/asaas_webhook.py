from __future__ import annotations

import hmac
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas import AsaasGateway, AsaasGatewayError
from app.billing.webhooks import BillingWebhookService
from app.core.config import AsaasConfigurationError, Settings, get_settings
from app.core.database import get_db

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]


@router.post("/asaas", status_code=200)
async def asaas_webhook(
    request: Request,
    settings: Config,
    db: Db,
    webhook_token: str | None = Header(default=None, alias="asaas-access-token"),
):
    try:
        expected = settings.require_asaas_webhook_token()
        configuration = settings.require_asaas_configuration()
    except AsaasConfigurationError:
        raise HTTPException(503, "Billing webhook is unavailable") from None
    if webhook_token is None or not hmac.compare_digest(webhook_token, expected):
        raise HTTPException(401, "Unauthorized webhook")

    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "Webhook payload too large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid webhook payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid webhook payload")

    try:
        await BillingWebhookService(db, AsaasGateway(configuration)).process(payload)
    except AsaasGatewayError:
        # A provider retry is desirable while a paid checkout is still waiting
        # for the associated subscription/payment to become queryable.
        raise HTTPException(503, "Billing reconciliation pending") from None
    return Response(status_code=200)

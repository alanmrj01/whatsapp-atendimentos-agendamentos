from __future__ import annotations

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.asaas import AsaasGateway
from app.billing.webhooks import BillingWebhookService
from app.core.config import AsaasConfigurationError, Settings, get_settings
from app.core.database import get_db
from app.push.service import WebPushDispatchError, dispatch_pending_web_push

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)
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

    # Keep the webhook path deterministic and local: provider webhooks are the
    # source of truth for checkout/payment state, so no secondary Asaas polling
    # is performed before acknowledging the delivery.
    action_event_key = await BillingWebhookService(
        db, AsaasGateway(configuration)
    ).process(payload)
    if action_event_key is not None:
        try:
            await dispatch_pending_web_push(
                db,
                action_event_key,
                settings,
            )
        except WebPushDispatchError:
            # Billing state is authoritative. A transient notification failure
            # must not make Asaas retry an already-applied financial event.
            logger.warning("billing_action_push_delivery_failed")
    return Response(status_code=200)

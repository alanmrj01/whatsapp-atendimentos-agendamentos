from __future__ import annotations

import hmac
import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.posthog import (
    PostHogEvent,
    business_distinct_id,
    business_owner_user_id,
    capture_posthog_event,
    user_distinct_id,
)
from app.billing.asaas import AsaasGateway
from app.billing.webhooks import BillingWebhookService
from app.core.config import AsaasConfigurationError, Settings, get_settings
from app.core.database import get_db
from app.models import BillingWebhookEvent
from app.push.service import WebPushDispatchError, dispatch_pending_web_push

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]


@router.post("/asaas", status_code=200)
async def asaas_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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
    billing_service = BillingWebhookService(
        db, AsaasGateway(configuration)
    )
    action_event_keys = await billing_service.process(payload)

    provider_event_id = payload.get("id")
    provider_event_type = payload.get("event")
    if (
        settings.posthog_enabled
        and settings.posthog_project_api_key is not None
        and isinstance(provider_event_id, str)
        and isinstance(provider_event_type, str)
    ):
        storage_event_id = billing_service._event_storage_id(
            billing_service.provider_environment,
            provider_event_id,
        )
        analytics_row = await db.get(BillingWebhookEvent, storage_event_id)
        if (
            analytics_row is not None
            and analytics_row.processed_at is not None
            and analytics_row.business_id is not None
        ):
            owner_user_id = await business_owner_user_id(
                db, analytics_row.business_id
            )
            distinct_id = (
                user_distinct_id(owner_user_id)
                if owner_user_id is not None
                else business_distinct_id(analytics_row.business_id)
            )
            background_tasks.add_task(
                capture_posthog_event,
                settings,
                PostHogEvent(
                    name=f"billing_{provider_event_type.lower()}",
                    distinct_id=distinct_id,
                    insert_id=f"billing:{storage_event_id}",
                    occurred_at=analytics_row.processed_at,
                    process_person_profile=owner_user_id is not None,
                    properties={
                        "analytics_source": "asaas_webhook",
                        "business_id": str(analytics_row.business_id),
                        "billing_event_type": provider_event_type,
                        "provider_environment": analytics_row.provider_environment,
                        "resource_type": analytics_row.resource_type,
                        "amount_cents": analytics_row.amount_cents,
                    },
                ),
            )
    for event_key in action_event_keys:
        try:
            await dispatch_pending_web_push(db, event_key, settings)
        except WebPushDispatchError:
            # Billing state is already reconciled and committed. A transient
            # push failure must not make Asaas retry a financial webhook.
            pass
    return Response(status_code=200)

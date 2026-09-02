from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.dependencies import get_booking_availability_port
from app.conversations.ports import BookingAvailabilityPort
from app.core.config import (
    CloudTasksConfigurationError,
    MetaConfigurationError,
    Settings,
    get_settings,
)
from app.core.database import get_db
from app.schemas.whatsapp_webhook import (
    WebhookAcknowledgement,
    WhatsAppWebhookPayload,
)
from app.tasks.cloud_tasks import (
    CloudTasksEnqueueError,
    CloudTasksEventEnqueuer,
    EventTaskEnqueuer,
)
from app.tasks.outbound import (
    build_outbound_task_enqueuer,
    enqueue_pending_outbounds_for_events,
)
from app.whatsapp.processor import (
    persist_webhook_events_for_tasks,
    process_webhook_events,
)
from app.whatsapp.webhook import (
    InboundMessageEvent,
    is_individual_whatsapp_id,
    normalize_webhook_payload,
    verify_meta_signature,
)

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp-webhook"])


def build_event_task_enqueuer(settings: Settings) -> EventTaskEnqueuer:
    return CloudTasksEventEnqueuer(
        settings.require_cloud_tasks_configuration()
    )


@router.get("", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(
    hub_mode: Annotated[
        str, Query(alias="hub.mode", min_length=1, max_length=64)
    ],
    hub_verify_token: Annotated[
        str, Query(alias="hub.verify_token", min_length=1, max_length=512)
    ],
    hub_challenge: Annotated[
        str, Query(alias="hub.challenge", min_length=1, max_length=1024)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PlainTextResponse:
    try:
        expected_token = settings.require_meta_verify_token()
    except MetaConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook unavailable",
        ) from None
    token_is_valid = secrets.compare_digest(hub_verify_token, expected_token)

    if hub_mode != "subscribe" or not token_is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook verification failed",
        )

    return PlainTextResponse(content=hub_challenge, status_code=status.HTTP_200_OK)


@router.post("", response_model=WebhookAcknowledgement)
async def receive_whatsapp_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db)],
    booking_port: Annotated[
        BookingAvailabilityPort,
        Depends(get_booking_availability_port),
    ],
    x_hub_signature_256: Annotated[
        str | None, Header(alias="X-Hub-Signature-256")
    ] = None,
) -> WebhookAcknowledgement:
    raw_body = await request.body()
    try:
        app_secret = settings.require_meta_app_secret()
    except MetaConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook unavailable",
        ) from None
    if not verify_meta_signature(raw_body, x_hub_signature_256, app_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        payload = WhatsAppWebhookPayload.model_validate_json(raw_body)
    except ValidationError:
        raise HTTPException(
            status_code=422,
            detail="Invalid request",
        ) from None

    events = normalize_webhook_payload(payload)
    if not settings.cloud_tasks_enabled:
        await process_webhook_events(session, events, booking_port=booking_port)
        has_individual_inbound = any(
            isinstance(event, InboundMessageEvent)
            and is_individual_whatsapp_id(event.whatsapp_id)
            for event in events
        )
        if settings.outbound_tasks_enabled and has_individual_inbound:
            try:
                outbound_enqueuer = build_outbound_task_enqueuer(settings)
                await enqueue_pending_outbounds_for_events(
                    session,
                    events,
                    outbound_enqueuer,
                )
            except (CloudTasksConfigurationError, CloudTasksEnqueueError):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Webhook processing unavailable",
                ) from None
        return WebhookAcknowledgement(status="accepted")

    try:
        enqueuer = build_event_task_enqueuer(settings)
    except CloudTasksConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing unavailable",
        ) from None

    event_keys = await persist_webhook_events_for_tasks(session, events)
    try:
        for event_key in event_keys:
            await enqueuer.enqueue(event_key)
    except CloudTasksEnqueueError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing unavailable",
        ) from None
    return WebhookAcknowledgement(status="accepted")

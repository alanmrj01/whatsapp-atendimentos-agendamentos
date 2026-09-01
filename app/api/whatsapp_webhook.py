from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.dependencies import get_booking_availability_port
from app.conversations.ports import BookingAvailabilityPort
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.schemas.whatsapp_webhook import (
    WebhookAcknowledgement,
    WhatsAppWebhookPayload,
)
from app.whatsapp.processor import process_webhook_events
from app.whatsapp.webhook import normalize_webhook_payload, verify_meta_signature

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp-webhook"])


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
    expected_token = settings.meta_verify_token.get_secret_value()
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
    app_secret = settings.meta_app_secret.get_secret_value()
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
    await process_webhook_events(session, events, booking_port=booking_port)
    return WebhookAcknowledgement(status="accepted")

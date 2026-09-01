from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from app.core.config import Settings, get_settings
from app.schemas.whatsapp_webhook import (
    WebhookAcknowledgement,
    WhatsAppWebhookPayload,
)

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
    _: WhatsAppWebhookPayload,
) -> WebhookAcknowledgement:
    # O conteúdo não é processado nem registrado nesta etapa da infraestrutura.
    return WebhookAcknowledgement(status="accepted")

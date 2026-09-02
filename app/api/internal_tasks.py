from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.dependencies import get_booking_availability_port
from app.conversations.ports import BookingAvailabilityPort
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.schemas.cloud_tasks import (
    TaskAcknowledgement,
    WhatsAppEventTaskPayload,
    WhatsAppOutboundTaskPayload,
)
from app.tasks.auth import (
    require_cloud_tasks_oidc,
    require_outbound_tasks_oidc,
)
from app.tasks.outbound import (
    build_outbound_task_enqueuer,
    enqueue_pending_outbounds_for_event,
    process_outbound_message,
)
from app.tasks.worker import process_cloud_task_event
from app.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/tasks", tags=["internal-tasks"])


@router.post(
    "/whatsapp-event",
    response_model=TaskAcknowledgement,
    dependencies=[Depends(require_cloud_tasks_oidc)],
)
async def process_whatsapp_event_task(
    payload: WhatsAppEventTaskPayload,
    session: Annotated[AsyncSession, Depends(get_db)],
    booking_port: Annotated[
        BookingAvailabilityPort,
        Depends(get_booking_availability_port),
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskAcknowledgement:
    try:
        await process_cloud_task_event(
            session,
            payload.event_key,
            booking_port=booking_port,
        )
        if settings.outbound_tasks_enabled:
            outbound_enqueuer = build_outbound_task_enqueuer(settings)
            await enqueue_pending_outbounds_for_event(
                session,
                payload.event_key,
                outbound_enqueuer,
            )
    except Exception as exc:
        logger.warning(
            "cloud_task_processing_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task processing failed",
        ) from None
    return TaskAcknowledgement(status="accepted")


@router.post(
    "/whatsapp-outbound",
    response_model=TaskAcknowledgement,
    dependencies=[Depends(require_outbound_tasks_oidc)],
)
async def process_whatsapp_outbound_task(
    payload: WhatsAppOutboundTaskPayload,
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TaskAcknowledgement:
    try:
        await process_outbound_message(
            session,
            payload.message_id,
            client_factory=lambda: WhatsAppClient(settings),
        )
    except Exception as exc:
        logger.warning(
            "outbound_task_processing_failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task processing failed",
        ) from None
    return TaskAcknowledgement(status="accepted")

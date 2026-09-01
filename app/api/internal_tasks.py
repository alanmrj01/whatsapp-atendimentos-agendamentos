from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.dependencies import get_booking_availability_port
from app.conversations.ports import BookingAvailabilityPort
from app.core.database import get_db
from app.schemas.cloud_tasks import TaskAcknowledgement, WhatsAppEventTaskPayload
from app.tasks.auth import require_cloud_tasks_oidc
from app.tasks.worker import process_cloud_task_event

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
) -> TaskAcknowledgement:
    try:
        await process_cloud_task_event(
            session,
            payload.event_key,
            booking_port=booking_port,
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

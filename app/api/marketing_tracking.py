from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin
from app.core.database import get_db
from app.models.marketing import MarketingFunnelEvent

router = APIRouter(prefix="/api/v1", tags=["marketing-tracking"])
Db = Annotated[AsyncSession, Depends(get_db)]


class MarketingEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    occurred_at: datetime | None = None
    visitor_id: UUID
    session_id: UUID
    event_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    surface: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_.-]+$")
    path: str = Field(min_length=1, max_length=255)
    business_id: UUID | None = None
    user_id: UUID | None = None
    referrer: str | None = Field(default=None, max_length=512)
    properties: dict[str, Any] | None = None


def _safe_properties(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    safe: dict[str, Any] = {}
    for key, item in list(value.items())[:48]:
        if not isinstance(key, str) or len(key) > 64:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            if isinstance(item, str):
                safe[key] = item[:256]
            else:
                safe[key] = item
    return safe or None


def _safe_occurred_at(value: datetime | None) -> datetime:
    now = datetime.now(UTC)
    if value is None or value.tzinfo is None:
        return now
    normalized = value.astimezone(UTC)
    if normalized > now + timedelta(minutes=5):
        return now
    if normalized < now - timedelta(days=90):
        return now
    return normalized


@router.post(
    "/marketing/events",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_origin)],
)
async def record_marketing_event(
    payload: MarketingEventRequest,
    request: Request,
    db: Db,
):
    # First-party product analytics only. We intentionally do not persist IP,
    # user-agent, cookies, access tokens, message content or form values here.
    client_event_id = payload.event_id or uuid4()
    statement = (
        insert(MarketingFunnelEvent)
        .values(
            id=uuid4(),
            client_event_id=client_event_id,
            occurred_at=_safe_occurred_at(payload.occurred_at),
            visitor_id=payload.visitor_id,
            session_id=payload.session_id,
            business_id=payload.business_id,
            user_id=payload.user_id,
            event_name=payload.event_name,
            surface=payload.surface,
            path=payload.path,
            referrer=payload.referrer,
            properties=_safe_properties(payload.properties),
        )
        .on_conflict_do_nothing(
            index_elements=[MarketingFunnelEvent.client_event_id]
        )
    )
    await db.execute(statement)
    await db.commit()
    return {"accepted": True, "request_id": getattr(request.state, "request_id", None)}

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.posthog import PostHogEvent, capture_posthog_event, visitor_distinct_id
from app.auth.dependencies import require_origin
from app.core.config import Environment, Settings, get_settings
from app.core.database import get_db
from app.models.marketing import MarketingFunnelEvent

router = APIRouter(prefix="/api/v1", tags=["marketing-tracking"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]

VISITOR_COOKIE = "alovia_analytics_vid"
SESSION_COOKIE = "alovia_analytics_sid"
ANALYTICS_COOKIE_PATH = "/api/v1/marketing"
VISITOR_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SESSION_MAX_AGE_SECONDS = 30 * 60


class MarketingEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    occurred_at: datetime | None = None
    visitor_id: UUID | None = None
    session_id: UUID | None = None
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


def _cookie_uuid(request: Request, name: str) -> UUID | None:
    raw = request.cookies.get(name)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _set_identity_cookie(
    response: Response,
    *,
    name: str,
    value: UUID,
    max_age: int,
    secure: bool,
) -> None:
    response.set_cookie(
        name,
        str(value),
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=ANALYTICS_COOKIE_PATH,
    )


@router.post(
    "/marketing/events",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_origin)],
)
async def record_marketing_event(
    payload: MarketingEventRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    settings: Config,
    db: Db,
):
    # First-party product analytics only. We intentionally do not persist IP,
    # user-agent, auth tokens, message content or form values here. Durable
    # analytics identity lives only in HttpOnly first-party cookies.
    visitor_id = (
        payload.visitor_id
        or _cookie_uuid(request, VISITOR_COOKIE)
        or uuid4()
    )
    session_id = (
        payload.session_id
        or _cookie_uuid(request, SESSION_COOKIE)
        or uuid4()
    )
    client_event_id = payload.event_id or uuid4()
    occurred_at = _safe_occurred_at(payload.occurred_at)
    safe_properties = _safe_properties(payload.properties)
    statement = (
        insert(MarketingFunnelEvent)
        .values(
            id=uuid4(),
            client_event_id=client_event_id,
            occurred_at=occurred_at,
            visitor_id=visitor_id,
            session_id=session_id,
            business_id=payload.business_id,
            user_id=payload.user_id,
            event_name=payload.event_name,
            surface=payload.surface,
            path=payload.path,
            referrer=payload.referrer,
            properties=safe_properties,
        )
        .on_conflict_do_nothing(
            index_elements=[MarketingFunnelEvent.client_event_id]
        )
    )
    await db.execute(statement)
    await db.commit()

    background_tasks.add_task(
        capture_posthog_event,
        settings,
        PostHogEvent(
            name=payload.event_name,
            distinct_id=visitor_distinct_id(visitor_id),
            insert_id=str(client_event_id),
            occurred_at=occurred_at,
            process_person_profile=False,
            properties={
                **(safe_properties or {}),
                "analytics_source": "alovia_backend",
                "surface": payload.surface,
                "path": payload.path,
                "session_id": str(session_id),
                "business_id": str(payload.business_id) if payload.business_id else None,
                "user_id": str(payload.user_id) if payload.user_id else None,
            },
        ),
    )

    secure = settings.environment is Environment.production
    _set_identity_cookie(
        response,
        name=VISITOR_COOKIE,
        value=visitor_id,
        max_age=VISITOR_MAX_AGE_SECONDS,
        secure=secure,
    )
    # Sliding 30-minute session keeps session-level attribution meaningful
    # without exposing an identifier to JavaScript.
    _set_identity_cookie(
        response,
        name=SESSION_COOKIE,
        value=session_id,
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=secure,
    )
    return {"accepted": True, "request_id": getattr(request.state, "request_id", None)}

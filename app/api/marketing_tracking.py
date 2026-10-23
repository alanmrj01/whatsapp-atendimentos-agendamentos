from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.posthog import (
    PostHogEvent,
    capture_posthog_event,
    identity_alias_event,
    visitor_distinct_id,
)
from app.auth.dependencies import optional_principal, require_origin
from app.auth.service import Principal
from app.core.config import Environment, Settings, get_settings
from app.core.database import get_db
from app.models.marketing import MarketingFunnelEvent

router = APIRouter(prefix="/api/v1", tags=["marketing-tracking"])
Db = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]

VISITOR_COOKIE = "alovia_analytics_vid"
SESSION_COOKIE = "alovia_analytics_sid"
ATTRIBUTION_COOKIE = "alovia_analytics_attr"
ANALYTICS_COOKIE_PATH = "/api/v1/marketing"
VISITOR_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SESSION_MAX_AGE_SECONDS = 30 * 60
ATTRIBUTION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
ATTRIBUTION_VALUE_MAX_LENGTH = 120
ATTRIBUTION_PROPERTY_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_id",
    "utm_source_platform",
    "utm_creative_format",
    "campaign_id",
    "adset_id",
    "ad_id",
    "placement",
    "fbclid",
    "gclid",
    "referrer_host",
    "landing_path",
    "attribution_version",
)
ATTRIBUTION_TOUCH_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "utm_source_platform",
        "utm_creative_format",
        "campaign_id",
        "adset_id",
        "ad_id",
        "placement",
        "fbclid",
        "gclid",
    }
)


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


def _safe_attribution(properties: dict[str, Any] | None) -> dict[str, Any]:
    if not properties:
        return {}
    safe: dict[str, Any] = {}
    for key in ATTRIBUTION_PROPERTY_KEYS:
        value = properties.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                safe[key] = normalized[:ATTRIBUTION_VALUE_MAX_LENGTH]
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[key] = value
    return safe


def _decode_attribution_cookie(value: str | None) -> dict[str, Any]:
    if not value or len(value) > 4096:
        return {}
    try:
        padding = "=" * ((4 - len(value) % 4) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode()).decode()
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return _safe_attribution(payload if isinstance(payload, dict) else None)


def _encode_attribution_cookie(properties: dict[str, Any]) -> str:
    encoded = json.dumps(
        _safe_attribution(properties),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _resolve_attribution(
    properties: dict[str, Any] | None,
    cookie_value: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    incoming = _safe_attribution(properties)
    has_new_touch = any(key in incoming for key in ATTRIBUTION_TOUCH_KEYS)
    attribution = incoming if has_new_touch else _decode_attribution_cookie(cookie_value)
    if not attribution:
        return properties, None

    merged = dict(properties or {})
    # Attribution values describe the acquisition touch, not the current route.
    # Reapply them after the user leaves the landing page so conversion events
    # keep the campaign that brought the visitor into this analytics journey.
    merged.update(attribution)
    next_cookie = _encode_attribution_cookie(attribution) if has_new_touch else None
    return merged, next_cookie


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
    principal: Annotated[Principal | None, Depends(optional_principal)],
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
    safe_properties, attribution_cookie_value = _resolve_attribution(
        safe_properties,
        request.cookies.get(ATTRIBUTION_COOKIE),
    )

    # Identity used for analytics must be derived from the verified server-side
    # principal, never trusted from the public request body. This keeps the
    # pre-signup endpoint anonymous while preventing forged PostHog merges.
    effective_user_id = principal.user.id if principal is not None else None
    effective_business_id: UUID | None = None
    if principal is not None:
        authorized_business_ids = {
            membership.business_id for membership in principal.memberships
        }
        if payload.business_id in authorized_business_ids:
            effective_business_id = payload.business_id
        elif principal.session.active_business_id in authorized_business_ids:
            effective_business_id = principal.session.active_business_id

    statement = (
        insert(MarketingFunnelEvent)
        .values(
            id=uuid4(),
            client_event_id=client_event_id,
            occurred_at=occurred_at,
            visitor_id=visitor_id,
            session_id=session_id,
            business_id=effective_business_id,
            user_id=effective_user_id,
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

    if effective_user_id is not None:
        background_tasks.add_task(
            capture_posthog_event,
            settings,
            identity_alias_event(visitor_id, effective_user_id),
        )

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
                "business_id": (
                    str(effective_business_id) if effective_business_id else None
                ),
                "user_id": str(effective_user_id) if effective_user_id else None,
            },
        ),
    )

    secure = settings.environment is Environment.production
    if attribution_cookie_value is not None:
        response.set_cookie(
            name=ATTRIBUTION_COOKIE,
            value=attribution_cookie_value,
            max_age=ATTRIBUTION_MAX_AGE_SECONDS,
            httponly=True,
            secure=secure,
            samesite="lax",
            path=ANALYTICS_COOKIE_PATH,
        )
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

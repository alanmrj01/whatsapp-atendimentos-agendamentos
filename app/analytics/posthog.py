from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Appointment, BusinessUserMembership, ProcessedWebhook

logger = logging.getLogger(__name__)
AUTOMATIC_BOOKING_SESSION_INFO_KEY = "posthog_automatic_booking_ids"


@dataclass(frozen=True, slots=True)
class PostHogEvent:
    name: str
    distinct_id: str
    properties: dict[str, Any]
    insert_id: str
    occurred_at: datetime | None = None
    process_person_profile: bool = False


def visitor_distinct_id(visitor_id: UUID) -> str:
    return f"visitor:{visitor_id}"


def user_distinct_id(user_id: UUID) -> str:
    return f"user:{user_id}"


def business_distinct_id(business_id: UUID) -> str:
    return f"business:{business_id}"


def identity_alias_event(visitor_id: UUID, user_id: UUID) -> PostHogEvent:
    visitor = visitor_distinct_id(visitor_id)
    user = user_distinct_id(user_id)
    return PostHogEvent(
        name="$create_alias",
        distinct_id=visitor,
        insert_id=f"identity:{visitor}:{user}",
        process_person_profile=True,
        properties={"alias": user},
    )


def queue_automatic_booking_posthog(session: AsyncSession, appointment_id: UUID) -> None:
    pending = session.info.setdefault(AUTOMATIC_BOOKING_SESSION_INFO_KEY, set())
    pending.add(appointment_id)


def take_automatic_booking_posthog_ids(session: AsyncSession) -> list[UUID]:
    raw = session.info.pop(AUTOMATIC_BOOKING_SESSION_INFO_KEY, set())
    return sorted(
        (value for value in raw if isinstance(value, UUID)),
        key=str,
    )


def automatic_booking_event(
    *,
    appointment_id: UUID,
    business_id: UUID,
    service_id: UUID,
    owner_user_id: UUID | None,
) -> PostHogEvent:
    distinct_id = (
        user_distinct_id(owner_user_id)
        if owner_user_id is not None
        else business_distinct_id(business_id)
    )
    return PostHogEvent(
        name="automatic_booking_confirmed",
        distinct_id=distinct_id,
        insert_id=f"automatic_booking:{appointment_id}",
        process_person_profile=owner_user_id is not None,
        properties={
            "appointment_id": str(appointment_id),
            "business_id": str(business_id),
            "service_id": str(service_id),
            "booking_source": "whatsapp_automation",
        },
    )


async def automatic_booking_events(
    db: AsyncSession,
    appointment_ids: list[UUID],
) -> list[PostHogEvent]:
    if not appointment_ids:
        return []
    rows = (
        await db.execute(
            select(
                Appointment.id,
                Appointment.business_id,
                Appointment.service_id,
            ).where(Appointment.id.in_(list(dict.fromkeys(appointment_ids))))
        )
    ).all()
    owners = await _owner_user_ids(db, {row.business_id for row in rows})
    return [
        automatic_booking_event(
            appointment_id=row.id,
            business_id=row.business_id,
            service_id=row.service_id,
            owner_user_id=owners.get(row.business_id),
        )
        for row in rows
    ]


SENSITIVE_PROPERTY_FRAGMENTS = (
    "email",
    "phone",
    "telefone",
    "cpf",
    "cnpj",
    "card",
    "ccv",
    "cvv",
    "token",
    "secret",
    "password",
    "message",
    "body",
    "pix_payload",
    "qr_code",
    "whatsapp_id",
)


def _safe_posthog_properties(properties: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in list(properties.items())[:64]:
        if not isinstance(key, str) or not key or len(key) > 80:
            continue
        lowered = key.lower()
        if any(fragment in lowered for fragment in SENSITIVE_PROPERTY_FRAGMENTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value[:256] if isinstance(value, str) else value
    return safe


def build_capture_payload(settings: Settings, event: PostHogEvent) -> dict[str, Any] | None:
    if not settings.posthog_enabled or settings.posthog_project_api_key is None:
        return None
    api_key = settings.posthog_project_api_key.get_secret_value().strip()
    if not api_key:
        return None

    properties = _safe_posthog_properties(event.properties)
    properties["$insert_id"] = event.insert_id[:200]
    properties["$process_person_profile"] = event.process_person_profile

    payload: dict[str, Any] = {
        "api_key": api_key,
        "event": event.name[:200],
        "distinct_id": event.distinct_id[:200],
        "properties": properties,
    }
    if event.occurred_at is not None:
        payload["timestamp"] = event.occurred_at.isoformat()
    return payload


async def capture_posthog_event(settings: Settings, event: PostHogEvent) -> None:
    payload = build_capture_payload(settings, event)
    if payload is None:
        return

    host = settings.posthog_host.strip().rstrip("/")
    if not host.startswith("https://"):
        logger.warning("posthog_capture_skipped_invalid_host")
        return

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(f"{host}/i/v0/e/", json=payload)
        if response.status_code >= 400:
            logger.warning(
                "posthog_capture_failed",
                extra={"status_code": response.status_code},
            )
    except (httpx.HTTPError, TimeoutError):
        logger.warning("posthog_capture_failed")


async def _owner_user_ids(
    db: AsyncSession, business_ids: set[UUID]
) -> dict[UUID, UUID]:
    if not business_ids:
        return {}
    rows = (
        await db.execute(
            select(
                BusinessUserMembership.business_id,
                BusinessUserMembership.user_id,
            ).where(
                BusinessUserMembership.business_id.in_(business_ids),
                BusinessUserMembership.role == "owner",
            )
        )
    ).all()
    return {business_id: user_id for business_id, user_id in rows}


async def whatsapp_outcome_events(
    db: AsyncSession,
    event_keys: list[str],
) -> list[PostHogEvent]:
    if not event_keys:
        return []

    rows = (
        await db.execute(
            select(ProcessedWebhook).where(
                ProcessedWebhook.event_key.in_(list(dict.fromkeys(event_keys)))
            )
        )
    ).scalars().all()
    business_ids = {row.business_id for row in rows if row.business_id is not None}
    owners = await _owner_user_ids(db, business_ids)

    events: list[PostHogEvent] = []
    for row in rows:
        if row.business_id is None:
            continue
        owner_user_id = owners.get(row.business_id)
        distinct_id = (
            user_distinct_id(owner_user_id)
            if owner_user_id is not None
            else business_distinct_id(row.business_id)
        )
        events.append(
            PostHogEvent(
                name="whatsapp_event_outcome",
                distinct_id=distinct_id,
                insert_id=(
                    f"whatsapp:{row.event_key}:{row.status}:"
                    f"{row.outcome_reason or 'none'}"
                ),
                occurred_at=row.processed_at or row.received_at,
                process_person_profile=owner_user_id is not None,
                properties={
                    "business_id": str(row.business_id),
                    "event_status": row.status,
                    "event_type": row.event_type,
                    "outcome_reason": row.outcome_reason,
                    "provider_failure_code": row.provider_failure_code,
                },
            )
        )
    return events


async def business_owner_user_id(
    db: AsyncSession,
    business_id: UUID | None,
) -> UUID | None:
    if business_id is None:
        return None
    return await db.scalar(
        select(BusinessUserMembership.user_id).where(
            BusinessUserMembership.business_id == business_id,
            BusinessUserMembership.role == "owner",
        )
    )

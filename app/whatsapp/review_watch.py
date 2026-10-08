from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MetaConfigurationError, Settings
from app.models import Business, BusinessWhatsAppConnection, CommercialSubscription, WebPushEvent
from app.repositories.whatsapp_connections import connection_record
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.credentials import BusinessWhatsAppCredentialProvider


REVIEW_INTERVAL = timedelta(days=3)
BILLING_DUE_WINDOW = timedelta(days=3)


async def create_due_action_alerts(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 100,
) -> list[str]:
    now = datetime.now(UTC)
    keys = await _billing_alerts(session, now, limit=limit)
    keys.extend(await _review_alerts(session, settings, now, limit=limit))
    await session.commit()
    return keys


async def _billing_alerts(
    session: AsyncSession,
    now: datetime,
    *,
    limit: int,
) -> list[str]:
    subscriptions = list((await session.scalars(
        select(CommercialSubscription)
        .where(
            CommercialSubscription.status.in_(["active", "past_due"]),
            CommercialSubscription.provider_environment == "production",
        )
        .order_by(CommercialSubscription.access_until)
        .limit(limit)
    )).all())
    keys: list[str] = []
    for subscription in subscriptions:
        if subscription.status == "past_due":
            event_type = "billing_past_due"
            event_key = f"billing-past-due:{subscription.id}"
        elif now <= subscription.access_until <= now + BILLING_DUE_WINDOW:
            event_type = "billing_due"
            event_key = f"billing-due:{subscription.id}:{subscription.access_until.date().isoformat()}"
        else:
            continue
        if await _insert_event(
            session,
            business_id=subscription.business_id,
            event_key=event_key,
            event_type=event_type,
            target_path="/app/mais/plano",
        ):
            keys.append(event_key)
    return keys


async def _review_alerts(
    session: AsyncSession,
    settings: Settings,
    now: datetime,
    *,
    limit: int,
) -> list[str]:
    try:
        meta = settings.require_meta_embedded_signup_configuration()
    except MetaConfigurationError:
        return []

    rows = list((await session.execute(
        select(Business, BusinessWhatsAppConnection)
        .join(
            BusinessWhatsAppConnection,
            BusinessWhatsAppConnection.business_id == Business.id,
        )
        .where(
            Business.whatsapp_desired_mode == WhatsAppConnectionMode.COEXISTENCE.value,
            BusinessWhatsAppConnection.mode == WhatsAppConnectionMode.API_ONLY.value,
            BusinessWhatsAppConnection.status == WhatsAppConnectionStatus.CONNECTED.value,
            BusinessWhatsAppConnection.meta_waba_id.is_not(None),
            BusinessWhatsAppConnection.credential_secret_ref.is_not(None),
            or_(
                Business.whatsapp_review_status.is_(None),
                Business.whatsapp_review_status != "approved",
            ),
            or_(
                Business.whatsapp_review_checked_at.is_(None),
                Business.whatsapp_review_checked_at <= now - REVIEW_INTERVAL,
            ),
        )
        .order_by(Business.whatsapp_review_checked_at.asc().nullsfirst())
        .limit(limit)
    )).all())
    if not rows:
        return []

    provider = BusinessWhatsAppCredentialProvider(settings)
    keys: list[str] = []
    async with httpx.AsyncClient(
        base_url=f"https://graph.facebook.com/{meta.graph_version}/",
        timeout=httpx.Timeout(10.0, connect=5.0),
    ) as client:
        for business, connection in rows:
            try:
                token = await provider.resolve(connection_record(connection))
                response = await client.get(
                    str(connection.meta_waba_id),
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                    params={"fields": "account_review_status"},
                )
                if response.status_code < 200 or response.status_code >= 300:
                    continue
                payload = response.json()
                raw = payload.get("account_review_status") if isinstance(payload, dict) else None
                normalized = str(raw or "").strip().upper()
                business.whatsapp_review_checked_at = now
                business.whatsapp_review_status = {
                    "APPROVED": "approved",
                    "REJECTED": "rejected",
                    "PENDING": "pending",
                }.get(normalized, "unknown")
                if normalized != "APPROVED":
                    continue
                event_key = f"whatsapp-review-approved:{business.id}:{connection.meta_waba_id}"
                if await _insert_event(
                    session,
                    business_id=business.id,
                    event_key=event_key,
                    event_type="whatsapp_coexistence_ready",
                    target_path="/app/whatsapp?alterar=coexistence",
                ):
                    business.whatsapp_review_notified_at = now
                    keys.append(event_key)
            except Exception:
                # One provider/account failure must not block checks for others.
                continue
    return keys


async def _insert_event(
    session: AsyncSession,
    *,
    business_id,
    event_key: str,
    event_type: str,
    target_path: str,
) -> bool:
    inserted = await session.scalar(
        postgresql_insert(WebPushEvent)
        .values(
            id=uuid.uuid4(),
            business_id=business_id,
            event_key=event_key,
            event_type=event_type,
            target_path=target_path,
        )
        .on_conflict_do_nothing(
            constraint="uq_web_push_events_event_key"
        )
        .returning(WebPushEvent.id)
    )
    return inserted is not None

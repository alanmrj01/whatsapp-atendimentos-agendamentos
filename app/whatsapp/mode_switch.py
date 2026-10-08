from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import BusinessWhatsAppConnection, WebPushEvent
from app.repositories.whatsapp_connections import connection_record
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.credentials import GoogleSecretManagerCredentialProvider
from app.whatsapp.embedded_signup import (
    MetaEmbeddedSignupError,
    MetaEmbeddedSignupGateway,
)


RECHECK_INTERVAL = timedelta(days=3)


async def recheck_due_coexistence_preferences(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 100,
) -> list[str]:
    """Refresh Meta account-review signals for API-only tenants waiting on coexistence.

    Account review is only a signal for the assisted journey. Approval never
    changes the technical WhatsApp mode automatically.
    """
    now = datetime.now(UTC)
    candidates = list(
        (
            await session.scalars(
                select(BusinessWhatsAppConnection)
                .where(
                    BusinessWhatsAppConnection.status
                    == WhatsAppConnectionStatus.CONNECTED.value,
                    BusinessWhatsAppConnection.mode
                    != WhatsAppConnectionMode.COEXISTENCE.value,
                    BusinessWhatsAppConnection.preferred_mode
                    == WhatsAppConnectionMode.COEXISTENCE.value,
                    BusinessWhatsAppConnection.mode_switch_next_check_at.is_not(None),
                    BusinessWhatsAppConnection.mode_switch_next_check_at <= now,
                    BusinessWhatsAppConnection.meta_waba_id.is_not(None),
                    BusinessWhatsAppConnection.credential_secret_ref.is_not(None),
                )
                .order_by(
                    BusinessWhatsAppConnection.mode_switch_next_check_at,
                    BusinessWhatsAppConnection.id,
                )
                .limit(limit)
            )
        ).all()
    )
    if not candidates:
        return []

    configuration = settings.require_meta_embedded_signup_configuration()
    gateway = MetaEmbeddedSignupGateway(configuration)
    credentials = GoogleSecretManagerCredentialProvider()
    event_keys: list[str] = []

    try:
        for candidate in candidates:
            review_status: str | None = None
            try:
                access_token = await credentials.resolve(connection_record(candidate))
                review_status = await gateway.fetch_account_review_status(
                    candidate.meta_waba_id,
                    access_token,
                )
            except (WhatsAppConfigurationError, MetaEmbeddedSignupError):
                review_status = None

            current = await session.scalar(
                select(BusinessWhatsAppConnection)
                .where(BusinessWhatsAppConnection.id == candidate.id)
                .with_for_update()
            )
            if current is None:
                continue
            if (
                current.status != WhatsAppConnectionStatus.CONNECTED.value
                or current.mode == WhatsAppConnectionMode.COEXISTENCE.value
                or current.preferred_mode
                != WhatsAppConnectionMode.COEXISTENCE.value
            ):
                continue

            current.mode_switch_last_checked_at = now
            if review_status == "approved":
                current.meta_review_status = "approved"
                current.mode_switch_next_check_at = None
                requested_at = current.mode_switch_requested_at or now
                event_key = (
                    f"connection-action:{current.id}:"
                    f"{int(requested_at.timestamp())}"
                )
                inserted = await session.scalar(
                    postgresql_insert(WebPushEvent)
                    .values(
                        business_id=current.business_id,
                        event_key=event_key,
                        event_type="connection_action",
                        target_path="/app/whatsapp",
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_web_push_events_event_key"
                    )
                    .returning(WebPushEvent.event_key)
                )
                if inserted is not None:
                    event_keys.append(str(inserted))
            else:
                if review_status == "rejected":
                    current.meta_review_status = "rejected"
                current.mode_switch_next_check_at = now + RECHECK_INTERVAL

        await session.flush()
    finally:
        await gateway.aclose()

    return event_keys

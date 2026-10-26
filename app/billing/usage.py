from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.catalog import monthly_message_limit
from app.models import Business, BusinessAccess, CommercialSubscription, Message
from app.models.billing import billing_provider_environment

BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
_COUNTED_STATUSES = ("sent", "delivered", "read")


@dataclass(frozen=True, slots=True)
class MessageUsageSnapshot:
    plan: str | None
    period_start: datetime
    resets_at: datetime
    used: int
    limit: int | None
    quota_applies: bool

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return bool(
            self.quota_applies
            and self.limit is not None
            and self.used >= self.limit
        )

    @property
    def upgrade_plan(self) -> str | None:
        if self.exhausted and self.plan == "basic":
            return "plus"
        return None


def _monthly_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = now or datetime.now(UTC)
    local = moment.astimezone(BRAZIL_TZ)
    start_local = datetime(local.year, local.month, 1, tzinfo=BRAZIL_TZ)
    if local.month == 12:
        reset_local = datetime(local.year + 1, 1, 1, tzinfo=BRAZIL_TZ)
    else:
        reset_local = datetime(local.year, local.month + 1, 1, tzinfo=BRAZIL_TZ)
    return start_local.astimezone(UTC), reset_local.astimezone(UTC)


async def message_usage_snapshot(
    db: AsyncSession,
    business_id: UUID,
    *,
    lock_business: bool = False,
    now: datetime | None = None,
) -> MessageUsageSnapshot:
    period_start, resets_at = _monthly_window(now)

    if lock_business:
        locked_business = await db.scalar(
            select(Business.id)
            .where(Business.id == business_id)
            .with_for_update()
        )
        if locked_business is None:
            raise LookupError("Business not found")

    access = await db.get(BusinessAccess, business_id)
    if access is not None and access.admin_full_access:
        return MessageUsageSnapshot(
            plan=None,
            period_start=period_start,
            resets_at=resets_at,
            used=0,
            limit=None,
            quota_applies=False,
        )

    environment = billing_provider_environment()
    moment = now or datetime.now(UTC)
    subscription = await db.scalar(
        select(CommercialSubscription)
        .where(
            CommercialSubscription.business_id == business_id,
            CommercialSubscription.provider_environment == environment,
            CommercialSubscription.status.in_(("active", "canceled")),
            CommercialSubscription.access_until > moment,
        )
        .order_by(
            CommercialSubscription.created_at.desc(),
            CommercialSubscription.id.desc(),
        )
        .limit(1)
    )

    if subscription is None:
        # Preserve compatibility for legacy paid tenants and admin-granted pilots.
        # Free/preactivation accounts are already stopped by operational entitlement
        # before this quota becomes relevant.
        return MessageUsageSnapshot(
            plan=None,
            period_start=period_start,
            resets_at=resets_at,
            used=0,
            limit=None,
            quota_applies=False,
        )

    limit = monthly_message_limit(subscription.plan_code)
    used = int(
        await db.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status.in_(_COUNTED_STATUSES),
                Message.message_type != "reaction",
                Message.created_at >= period_start,
                Message.created_at < resets_at,
            )
        )
        or 0
    )

    return MessageUsageSnapshot(
        plan=subscription.plan_code,
        period_start=period_start,
        resets_at=resets_at,
        used=used,
        limit=limit,
        quota_applies=True,
    )

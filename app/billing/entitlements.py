from __future__ import annotations

from sqlalchemy import exists, func, or_, select

from app.models import BusinessAccess, CommercialSubscription
from app.models.billing import billing_provider_environment


def admin_full_access_exists(business_id):
    return exists(
        select(BusinessAccess.business_id).where(
            BusinessAccess.business_id == business_id,
            BusinessAccess.admin_full_access.is_(True),
        )
    )


def active_subscription_exists(
    business_id,
    provider_environment: str | None = None,
    *,
    at=None,
):
    environment = provider_environment or billing_provider_environment()
    moment = func.now() if at is None else at
    return exists(
        select(CommercialSubscription.id).where(
            CommercialSubscription.business_id == business_id,
            CommercialSubscription.provider_environment == environment,
            # past_due is deliberately excluded: overdue means blocked now.
            # canceled remains valid until the already-paid access_until.
            CommercialSubscription.status.in_(("active", "canceled")),
            CommercialSubscription.access_until > moment,
        )
    )


def legacy_paid_access_exists(business_id):
    """Preserve pre-commercial paid compatibility without granting unlimited access."""
    explicit_paid = exists(
        select(BusinessAccess.business_id).where(
            BusinessAccess.business_id == business_id,
            BusinessAccess.access_mode == "paid",
        )
    )
    # Missing rows were historically interpreted as paid. Preserve that
    # compatibility, but never treat it as an administrative full-access grant.
    missing_legacy_row = ~exists(
        select(BusinessAccess.business_id).where(
            BusinessAccess.business_id == business_id,
        )
    )
    return or_(explicit_paid, missing_legacy_row)


def active_operational_access_exists(
    business_id,
    provider_environment: str | None = None,
    *,
    at=None,
):
    return or_(
        admin_full_access_exists(business_id),
        legacy_paid_access_exists(business_id),
        active_subscription_exists(
            business_id,
            provider_environment,
            at=at,
        ),
    )


def plus_features_access_exists(
    business_id,
    provider_environment: str | None = None,
    *,
    at=None,
):
    environment = provider_environment or billing_provider_environment()
    moment = func.now() if at is None else at
    commercial_plus = exists(
        select(CommercialSubscription.id).where(
            CommercialSubscription.business_id == business_id,
            CommercialSubscription.provider_environment == environment,
            CommercialSubscription.plan_code == "plus",
            CommercialSubscription.status.in_(("active", "canceled")),
            CommercialSubscription.access_until > moment,
        )
    )
    return or_(admin_full_access_exists(business_id), commercial_plus)

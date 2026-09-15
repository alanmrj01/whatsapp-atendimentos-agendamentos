from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.domain import TimestampMixin, UUIDPrimaryKeyMixin


class BillingCheckout(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_checkouts"
    __table_args__ = (
        CheckConstraint("plan_code IN ('basic', 'plus')", name="billing_checkout_plan_allowed"),
        CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'annual')",
            name="billing_checkout_cycle_allowed",
        ),
        CheckConstraint(
            "payment_method IN ('credit_card', 'pix_automatic')",
            name="billing_checkout_payment_method_allowed",
        ),
        CheckConstraint(
            "status IN ('creating', 'active', 'paid', 'canceled', 'expired', 'failed')",
            name="billing_checkout_status_allowed",
        ),
        CheckConstraint("amount_cents > 0", name="billing_checkout_amount_positive"),
        Index("ix_billing_checkouts_business_id", "business_id"),
        Index("ix_billing_checkouts_provider_checkout_id", "provider_checkout_id", unique=True),
        Index("ix_billing_checkouts_provider_authorization_id", "provider_authorization_id", unique=True),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False)
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_cycle: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="creating", server_default=text("'creating'"), nullable=False
    )
    provider_checkout_id: Mapped[str | None] = mapped_column(String(80))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    provider_authorization_id: Mapped[str | None] = mapped_column(String(100))
    pix_conciliation_identifier: Mapped[str | None] = mapped_column(String(100))
    pix_qr_payload: Mapped[str | None] = mapped_column(Text)
    pix_qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_customer_id: Mapped[str | None] = mapped_column(String(80))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(80))


class CommercialSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "commercial_subscriptions"
    __table_args__ = (
        CheckConstraint("plan_code IN ('basic', 'plus')", name="commercial_subscription_plan_allowed"),
        CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'annual')",
            name="commercial_subscription_cycle_allowed",
        ),
        CheckConstraint(
            "payment_method IN ('credit_card', 'pix_automatic')",
            name="commercial_subscription_payment_method_allowed",
        ),
        CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'suspended')",
            name="commercial_subscription_status_allowed",
        ),
        CheckConstraint(
            "provider_subscription_id IS NOT NULL OR provider_authorization_id IS NOT NULL",
            name="commercial_subscription_provider_reference_required",
        ),
        Index("ix_commercial_subscriptions_business_id", "business_id"),
        Index("ix_commercial_subscriptions_provider_subscription_id", "provider_subscription_id", unique=True),
        Index("ix_commercial_subscriptions_provider_authorization_id", "provider_authorization_id", unique=True),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_checkouts.id"), nullable=False, unique=True
    )
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_cycle: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(80))
    provider_authorization_id: Mapped[str | None] = mapped_column(String(100))
    provider_customer_id: Mapped[str | None] = mapped_column(String(80))
    access_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingWebhookEvent(Base):
    __tablename__ = "billing_webhook_events"
    __table_args__ = (
        Index("ix_billing_webhook_events_provider_payment_id", "provider_payment_id"),
    )

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(100))
    provider_payment_id: Mapped[str | None] = mapped_column(String(100))
    provider_authorization_id: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

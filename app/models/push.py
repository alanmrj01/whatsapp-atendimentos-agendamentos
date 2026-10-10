from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.domain import TimestampMixin, UUIDPrimaryKeyMixin


class WebPushSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "business_id"],
            [
                "business_user_memberships.user_id",
                "business_user_memberships.business_id",
            ],
            name="fk_web_push_subscriptions_user_business_membership",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "business_id",
            "endpoint_hash",
            name="uq_web_push_subscriptions_business_endpoint",
        ),
        UniqueConstraint(
            "business_id",
            "id",
            name="uq_web_push_subscriptions_business_id_id",
        ),
        Index("ix_web_push_subscriptions_user_id", "user_id"),
        Index("ix_web_push_subscriptions_session_id", "auth_session_id"),
        Index("ix_web_push_subscriptions_business_id", "business_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    auth_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth_secret: Mapped[str] = mapped_column(Text, nullable=False)


class WebPushEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "web_push_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'inbound_message', 'automatic_booking', "
            "'human_intervention', 'connection_action', 'billing_action'"
            ")",
            name="event_type_allowed",
        ),
        UniqueConstraint("event_key", name="uq_web_push_events_event_key"),
        UniqueConstraint(
            "business_id",
            "id",
            name="uq_web_push_events_business_id_id",
        ),
        Index(
            "ix_web_push_events_business_processed_created",
            "business_id",
            "processed_at",
            "created_at",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WebPushDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "web_push_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name="status_allowed",
        ),
        ForeignKeyConstraint(
            ["business_id", "subscription_id"],
            ["web_push_subscriptions.business_id", "web_push_subscriptions.id"],
            name="fk_web_push_deliveries_business_subscription",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["business_id", "event_id"],
            ["web_push_events.business_id", "web_push_events.id"],
            name="fk_web_push_deliveries_business_event",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "subscription_id",
            "event_id",
            name="uq_web_push_deliveries_subscription_event",
        ),
        Index("ix_web_push_deliveries_business_id", "business_id"),
        Index("ix_web_push_deliveries_status", "status"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

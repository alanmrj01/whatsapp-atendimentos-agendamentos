from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.domain import TimestampMixin, UUIDPrimaryKeyMixin


class ReengagementDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reengagement_deliveries"
    __table_args__ = (
        CheckConstraint(
            "campaign IN ('upgrade', 'whatsapp_activation')",
            name="campaign_allowed",
        ),
        CheckConstraint("step BETWEEN 1 AND 3", name="step_allowed"),
        ForeignKeyConstraint(
            ["user_id", "business_id"],
            [
                "business_user_memberships.user_id",
                "business_user_memberships.business_id",
            ],
            name="fk_reengagement_deliveries_user_business_membership",
        ),
        UniqueConstraint(
            "user_id",
            "business_id",
            "campaign",
            "step",
            name="uq_reengagement_deliveries_user_business_campaign_step",
        ),
        Index(
            "ix_reengagement_deliveries_business_campaign",
            "business_id",
            "campaign",
            "step",
        ),
        Index(
            "ix_reengagement_deliveries_email_pending",
            "email_sent_at",
            "created_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign: Mapped[str] = mapped_column(String(32), nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    popup_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    popup_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cta_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

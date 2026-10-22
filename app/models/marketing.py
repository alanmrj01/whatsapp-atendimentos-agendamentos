from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, JSON, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketingFunnelEvent(Base):
    __tablename__ = "marketing_funnel_events"
    __table_args__ = (
        Index("ix_marketing_funnel_events_created", "created_at"),
        Index("ix_marketing_funnel_events_visitor_created", "visitor_id", "created_at"),
        Index("ix_marketing_funnel_events_business_created", "business_id", "created_at"),
        Index("ix_marketing_funnel_events_name_created", "event_name", "created_at"),
        Index("ix_marketing_funnel_events_event_occurred", "event_name", "occurred_at"),
        Index(
            "uq_marketing_funnel_events_client_event_id",
            "client_event_id",
            unique=True,
        ),
        Index(
            "ix_marketing_funnel_events_properties_gin",
            "properties",
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    visitor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    surface: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    referrer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    properties: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

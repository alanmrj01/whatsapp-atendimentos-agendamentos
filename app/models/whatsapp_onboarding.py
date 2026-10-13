from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WhatsAppOnboardingAttempt(Base):
    """Server-side source of truth for one Meta Embedded Signup attempt.

    No OAuth authorization code or access token is persisted here. The table
    stores only lifecycle metadata required for correlation, idempotency,
    expiry and diagnostics.
    """

    __tablename__ = "whatsapp_onboarding_attempts"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('coexistence', 'api_only')",
            name="mode_allowed",
        ),
        CheckConstraint(
            "state IN ("
            "'created','provider_opened','awaiting_provider','code_received',"
            "'assets_received','completing','connected',"
            "'authorization_incomplete','cancelled','failed','expired','superseded'"
            ")",
            name="state_allowed",
        ),
        CheckConstraint(
            "last_stage IS NULL OR last_stage ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="last_stage_sanitized",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="failure_code_sanitized",
        ),
        Index(
            "uq_whatsapp_onboarding_attempts_active_business",
            "business_id",
            unique=True,
            postgresql_where=text(
                "state IN ('created','provider_opened','awaiting_provider',"
                "'code_received','assets_received','completing')"
            ),
        ),
        Index(
            "uq_whatsapp_onboarding_attempts_request_id_present",
            "request_id",
            unique=True,
            postgresql_where=text("request_id IS NOT NULL"),
        ),
        Index(
            "ix_whatsapp_onboarding_attempts_business_created",
            "business_id",
            "created_at",
        ),
        Index(
            "ix_whatsapp_onboarding_attempts_expires_active",
            "expires_at",
            postgresql_where=text(
                "state IN ('created','provider_opened','awaiting_provider',"
                "'code_received','assets_received','completing')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "business_whatsapp_connections.id",
            name="fk_wa_onboarding_attempt_connection",
        ),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default="created", server_default="created", nullable=False
    )
    last_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authorization_code_received: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    waba_id_received: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    phone_number_id_received: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    empty_callback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    terminal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WhatsAppOnboardingAttemptEvent(Base):
    """Sanitized lifecycle event for one onboarding attempt.

    The event journal intentionally stores only boolean presence flags and
    state metadata. OAuth codes, access tokens and Meta asset identifiers are
    never persisted here.
    """

    __tablename__ = "whatsapp_onboarding_attempt_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('client','server')",
            name="source_allowed",
        ),
        CheckConstraint(
            "stage ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="stage_sanitized",
        ),
        CheckConstraint(
            "state_after ~ '^[A-Za-z0-9._-]{1,32}$'",
            name="state_after_sanitized",
        ),
        CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="failure_code_sanitized",
        ),
        Index(
            "ix_whatsapp_onboarding_attempt_events_attempt_created",
            "attempt_id",
            "created_at",
        ),
        Index(
            "ix_whatsapp_onboarding_attempt_events_business_created",
            "business_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "whatsapp_onboarding_attempts.id",
            name="fk_wa_onboarding_event_attempt",
        ),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    state_after: Mapped[str] = mapped_column(String(32), nullable=False)
    authorization_code_received: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    waba_id_received: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    phone_number_id_received: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.domain import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(trim(email)) AND length(email) > 3", name="email_normalized"),
        CheckConstraint("platform_role IS NULL OR platform_role = 'super_admin'", name="platform_role_allowed"),
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="password_hash_argon2id"),
    )
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    platform_role: Mapped[str | None] = mapped_column(String(20))


class BusinessUserMembership(Base):
    __tablename__ = "business_user_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'attendant', 'viewer')", name="role_allowed"),
        Index("ix_business_user_memberships_business_id", "business_id"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))


class AuthSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("refresh_token_hash ~ '^[0-9a-f]{64}$'", name="refresh_hash_format"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    # A session may outlive a revoked membership; authorization always joins the
    # current membership. No cascade may erase business history.
    active_business_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

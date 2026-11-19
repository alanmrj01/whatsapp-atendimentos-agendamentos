from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.domain import TimestampMixin, UUIDPrimaryKeyMixin


class McpOAuthAuthorizationCode(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    Base,
):
    __tablename__ = "mcp_oauth_authorization_codes"
    __table_args__ = (
        CheckConstraint(
            "code_hash ~ '^[0-9a-f]{64}$'",
            name="code_hash_format",
        ),
        CheckConstraint(
            "code_challenge ~ '^[A-Za-z0-9_-]{43,128}$'",
            name="code_challenge_format",
        ),
        CheckConstraint(
            "length(trim(scope)) > 0",
            name="scope_not_blank",
        ),
        CheckConstraint(
            "resource LIKE 'https://%'",
            name="resource_https",
        ),
        CheckConstraint(
            "redirect_uri LIKE 'https://%'",
            name="redirect_uri_https",
        ),
        Index(
            "ix_mcp_oauth_authorization_codes_expires_at",
            "expires_at",
        ),
        Index(
            "ix_mcp_oauth_authorization_codes_business_id",
            "business_id",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    code_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    code_challenge: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    resource: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )


class McpOAuthSession(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    Base,
):
    __tablename__ = "mcp_oauth_sessions"
    __table_args__ = (
        CheckConstraint(
            "refresh_token_hash ~ '^[0-9a-f]{64}$'",
            name="refresh_token_hash_format",
        ),
        CheckConstraint(
            "length(trim(scope)) > 0",
            name="scope_not_blank",
        ),
        CheckConstraint(
            "resource LIKE 'https://%'",
            name="resource_https",
        ),
        Index(
            "ix_mcp_oauth_sessions_expires_at",
            "expires_at",
        ),
        Index(
            "ix_mcp_oauth_sessions_business_id",
            "business_id",
        ),
        Index(
            "ix_mcp_oauth_sessions_user_id",
            "user_id",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    resource: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )


class McpOAuthAuthorizationFlow(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    Base,
):
    __tablename__ = "mcp_oauth_authorization_flows"
    __table_args__ = (
        CheckConstraint(
            "flow_token_hash ~ '^[0-9a-f]{64}$'",
            name="flow_token_hash_format",
        ),
        CheckConstraint(
            "length(state) > 0",
            name="state_not_blank",
        ),
        CheckConstraint(
            "code_challenge ~ '^[A-Za-z0-9_-]{43,128}$'",
            name="code_challenge_format",
        ),
        CheckConstraint(
            "length(trim(scope)) > 0",
            name="scope_not_blank",
        ),
        CheckConstraint(
            "resource LIKE 'https://%'",
            name="resource_https",
        ),
        CheckConstraint(
            "redirect_uri LIKE 'https://%'",
            name="redirect_uri_https",
        ),
        Index(
            "ix_mcp_oauth_authorization_flows_expires_at",
            "expires_at",
        ),
        Index(
            "ix_mcp_oauth_authorization_flows_user_id",
            "user_id",
        ),
    )

    flow_token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    code_challenge: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    resource: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

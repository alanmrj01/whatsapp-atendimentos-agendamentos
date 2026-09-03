"""Add business-scoped WhatsApp connections.

Revision ID: 20260902_0005
Revises: 20260902_0004
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0005"
down_revision: str | None = "20260902_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "business_whatsapp_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=16),
            server_default="meta",
            nullable=False,
        ),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("meta_waba_id", sa.String(length=255), nullable=True),
        sa.Column(
            "meta_phone_number_id", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "display_phone_number", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "credential_secret_ref", sa.String(length=512), nullable=True
        ),
        sa.Column("graph_version", sa.String(length=32), nullable=True),
        sa.Column(
            "connected_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "disconnected_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider = 'meta'",
            name=op.f("ck_business_whatsapp_connections_provider_allowed"),
        ),
        sa.CheckConstraint(
            "mode IN ('coexistence', 'api_only')",
            name=op.f("ck_business_whatsapp_connections_mode_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'disconnected', 'error')",
            name=op.f("ck_business_whatsapp_connections_status_allowed"),
        ),
        sa.CheckConstraint(
            "graph_version IS NULL OR "
            "graph_version ~ '^v[0-9]{1,3}\\.[0-9]{1,3}$'",
            name=op.f(
                "ck_business_whatsapp_connections_graph_version_format"
            ),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR "
            "last_error_code ~ '^[A-Za-z0-9._-]{1,64}$'",
            name=op.f(
                "ck_business_whatsapp_connections_last_error_code_sanitized"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f(
                "fk_business_whatsapp_connections_business_id_businesses"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_business_whatsapp_connections")
        ),
    )
    op.create_index(
        "ix_business_whatsapp_connections_business_status",
        "business_whatsapp_connections",
        ["business_id", "status"],
    )
    op.create_index(
        "uq_business_whatsapp_connections_active_business",
        "business_whatsapp_connections",
        ["business_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'disconnected'"),
    )
    op.create_index(
        "uq_business_whatsapp_connections_meta_phone_present",
        "business_whatsapp_connections",
        ["meta_phone_number_id"],
        unique=True,
        postgresql_where=sa.text("meta_phone_number_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_business_whatsapp_connections_meta_phone_present",
        table_name="business_whatsapp_connections",
        postgresql_where=sa.text("meta_phone_number_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_business_whatsapp_connections_active_business",
        table_name="business_whatsapp_connections",
        postgresql_where=sa.text("status <> 'disconnected'"),
    )
    op.drop_index(
        "ix_business_whatsapp_connections_business_status",
        table_name="business_whatsapp_connections",
    )
    op.drop_table("business_whatsapp_connections")

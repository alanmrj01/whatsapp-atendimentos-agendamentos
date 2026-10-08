"""Track WhatsApp mode preference and Meta review independently.

Revision ID: 20261008_0030
Revises: 20261004_0029
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa


revision = "20261008_0030"
down_revision = "20261004_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_whatsapp_connections",
        sa.Column("meta_review_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "business_whatsapp_connections",
        sa.Column("preferred_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "business_whatsapp_connections",
        sa.Column("mode_switch_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "business_whatsapp_connections",
        sa.Column("mode_switch_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "business_whatsapp_connections",
        sa.Column("mode_switch_next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "meta_review_status_allowed",
        "business_whatsapp_connections",
        "meta_review_status IS NULL OR meta_review_status IN ('approved', 'rejected')",
    )
    op.create_check_constraint(
        "preferred_mode_allowed",
        "business_whatsapp_connections",
        "preferred_mode IS NULL OR preferred_mode IN ('coexistence', 'api_only')",
    )
    op.create_index(
        "ix_business_whatsapp_connections_mode_switch_due",
        "business_whatsapp_connections",
        ["preferred_mode", "mode_switch_next_check_at"],
        postgresql_where=sa.text(
            "preferred_mode IS NOT NULL AND status = 'connected'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_business_whatsapp_connections_mode_switch_due",
        table_name="business_whatsapp_connections",
    )
    op.drop_constraint(
        "preferred_mode_allowed",
        "business_whatsapp_connections",
        type_="check",
    )
    op.drop_constraint(
        "meta_review_status_allowed",
        "business_whatsapp_connections",
        type_="check",
    )
    for column in (
        "mode_switch_next_check_at",
        "mode_switch_last_checked_at",
        "mode_switch_requested_at",
        "preferred_mode",
        "meta_review_status",
    ):
        op.drop_column("business_whatsapp_connections", column)

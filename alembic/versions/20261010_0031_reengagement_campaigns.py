"""Persist account reengagement delivery state.

Revision ID: 20261010_0031
Revises: 20261008_0030
Create Date: 2026-10-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261010_0031"
down_revision = "20261008_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reengagement_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign", sa.String(length=32), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("popup_shown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("popup_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cta_clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "campaign IN ('upgrade', 'whatsapp_activation')",
            name="ck_reengagement_deliveries_campaign_allowed",
        ),
        sa.CheckConstraint(
            "step BETWEEN 1 AND 3",
            name="ck_reengagement_deliveries_step_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "business_id"],
            ["business_user_memberships.user_id", "business_user_memberships.business_id"],
            name="fk_reengagement_deliveries_user_business_membership",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reengagement_deliveries"),
        sa.UniqueConstraint(
            "user_id", "business_id", "campaign", "step",
            name="uq_reengagement_deliveries_user_business_campaign_step",
        ),
    )
    op.create_index(
        "ix_reengagement_deliveries_business_campaign",
        "reengagement_deliveries",
        ["business_id", "campaign", "step"],
    )
    op.create_index(
        "ix_reengagement_deliveries_email_pending",
        "reengagement_deliveries",
        ["email_sent_at", "created_at"],
    )
    op.execute(
        'ALTER TABLE public."reengagement_deliveries" ENABLE ROW LEVEL SECURITY'
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE public."reengagement_deliveries" DISABLE ROW LEVEL SECURITY'
    )
    op.drop_index(
        "ix_reengagement_deliveries_email_pending",
        table_name="reengagement_deliveries",
    )
    op.drop_index(
        "ix_reengagement_deliveries_business_campaign",
        table_name="reengagement_deliveries",
    )
    op.drop_table("reengagement_deliveries")

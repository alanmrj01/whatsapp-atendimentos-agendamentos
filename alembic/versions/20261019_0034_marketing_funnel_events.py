"""Add first-party marketing funnel events.

Revision ID: 20261019_0034
Revises: 20261018_0033
Create Date: 2026-10-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261019_0034"
down_revision = "20261018_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical installations may still carry the SQLAlchemy-generated legacy
    # checkout constraint. Remove it defensively before adding new schema.
    op.execute(
        'ALTER TABLE billing_checkouts '
        'DROP CONSTRAINT IF EXISTS ck_billing_checkouts_billing_checkout_payment_method_allowed'
    )

    op.create_table(
        "marketing_funnel_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visitor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_name", sa.String(length=64), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("referrer", sa.String(length=512), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"],
            name="fk_marketing_funnel_events_business_id_businesses",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_marketing_funnel_events_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_marketing_funnel_events"),
    )
    op.create_index(
        "ix_marketing_funnel_events_created",
        "marketing_funnel_events",
        ["created_at"],
    )
    op.create_index(
        "ix_marketing_funnel_events_visitor_created",
        "marketing_funnel_events",
        ["visitor_id", "created_at"],
    )
    op.create_index(
        "ix_marketing_funnel_events_business_created",
        "marketing_funnel_events",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_marketing_funnel_events_name_created",
        "marketing_funnel_events",
        ["event_name", "created_at"],
    )
    op.execute('ALTER TABLE "marketing_funnel_events" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.execute('ALTER TABLE "marketing_funnel_events" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_marketing_funnel_events_name_created", table_name="marketing_funnel_events")
    op.drop_index("ix_marketing_funnel_events_business_created", table_name="marketing_funnel_events")
    op.drop_index("ix_marketing_funnel_events_visitor_created", table_name="marketing_funnel_events")
    op.drop_index("ix_marketing_funnel_events_created", table_name="marketing_funnel_events")
    op.drop_table("marketing_funnel_events")

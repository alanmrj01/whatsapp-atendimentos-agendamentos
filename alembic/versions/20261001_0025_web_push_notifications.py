"""Add tenant-scoped Web Push subscriptions and delivery outbox.

Revision ID: 20261001_0025
Revises: 20260930_0024
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0025"
down_revision = "20260930_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "auth_session_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("endpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth_secret", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["auth_session_id"],
            ["auth_sessions.id"],
            name=op.f(
                "fk_web_push_subscriptions_auth_session_id_auth_sessions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "business_id"],
            [
                "business_user_memberships.user_id",
                "business_user_memberships.business_id",
            ],
            name="fk_web_push_subscriptions_user_business_membership",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_web_push_subscriptions")
        ),
        sa.UniqueConstraint(
            "business_id",
            "endpoint_hash",
            name="uq_web_push_subscriptions_business_endpoint",
        ),
        sa.UniqueConstraint(
            "business_id",
            "id",
            name="uq_web_push_subscriptions_business_id_id",
        ),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_id",
        "web_push_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_web_push_subscriptions_session_id",
        "web_push_subscriptions",
        ["auth_session_id"],
    )
    op.create_index(
        "ix_web_push_subscriptions_business_id",
        "web_push_subscriptions",
        ["business_id"],
    )

    op.create_table(
        "web_push_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("target_path", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('inbound_message', 'automatic_booking')",
            name=op.f("ck_web_push_events_event_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_web_push_events_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_push_events")),
        sa.UniqueConstraint(
            "event_key", name="uq_web_push_events_event_key"
        ),
        sa.UniqueConstraint(
            "business_id",
            "id",
            name="uq_web_push_events_business_id_id",
        ),
    )
    op.create_index(
        "ix_web_push_events_business_processed_created",
        "web_push_events",
        ["business_id", "processed_at", "created_at"],
    )

    op.create_table(
        "web_push_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "subscription_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_web_push_deliveries_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "event_id"],
            ["web_push_events.business_id", "web_push_events.id"],
            name="fk_web_push_deliveries_business_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "subscription_id"],
            [
                "web_push_subscriptions.business_id",
                "web_push_subscriptions.id",
            ],
            name="fk_web_push_deliveries_business_subscription",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_push_deliveries")),
        sa.UniqueConstraint(
            "subscription_id",
            "event_id",
            name="uq_web_push_deliveries_subscription_event",
        ),
    )
    op.create_index(
        "ix_web_push_deliveries_business_id",
        "web_push_deliveries",
        ["business_id"],
    )
    op.create_index(
        "ix_web_push_deliveries_status",
        "web_push_deliveries",
        ["status"],
    )

    for table in (
        "web_push_subscriptions",
        "web_push_events",
        "web_push_deliveries",
    ):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in (
        "web_push_deliveries",
        "web_push_events",
        "web_push_subscriptions",
    ):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "ix_web_push_deliveries_status",
        table_name="web_push_deliveries",
    )
    op.drop_index(
        "ix_web_push_deliveries_business_id",
        table_name="web_push_deliveries",
    )
    op.drop_table("web_push_deliveries")
    op.drop_index(
        "ix_web_push_events_business_processed_created",
        table_name="web_push_events",
    )
    op.drop_table("web_push_events")
    op.drop_index(
        "ix_web_push_subscriptions_business_id",
        table_name="web_push_subscriptions",
    )
    op.drop_index(
        "ix_web_push_subscriptions_session_id",
        table_name="web_push_subscriptions",
    )
    op.drop_index(
        "ix_web_push_subscriptions_user_id",
        table_name="web_push_subscriptions",
    )
    op.drop_table("web_push_subscriptions")

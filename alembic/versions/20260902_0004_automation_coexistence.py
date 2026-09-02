"""Add business automation exclusions and temporary human control.

Revision ID: 20260902_0004
Revises: 20260901_0003
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0004"
down_revision: str | None = "20260901_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "human_control_window_minutes",
            sa.Integer(),
            server_default="2160",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_businesses_human_control_window_minutes_allowed"),
        "businesses",
        "human_control_window_minutes IN "
        "(5, 10, 20, 30, 60, 120, 240, 360, 720, 1440, 2160)",
    )

    op.add_column(
        "conversations",
        sa.Column(
            "automation_suppressed_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("suppression_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "human_control_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "last_human_message_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "conversation_initiated_by",
            sa.String(length=16),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_conversations_suppression_reason_allowed"),
        "conversations",
        "suppression_reason IS NULL OR "
        "suppression_reason = 'manual_business_message'",
    )
    op.create_check_constraint(
        op.f("ck_conversations_initiated_by_allowed"),
        "conversations",
        "conversation_initiated_by IS NULL OR "
        "conversation_initiated_by IN ('customer', 'business')",
    )
    op.create_index(
        "ix_conversations_automation_suppressed_until",
        "conversations",
        ["automation_suppressed_until"],
    )

    op.create_table(
        "business_automation_exclusions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("whatsapp_id", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
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
            "mode IN ('ignore', 'human_only')",
            name=op.f("ck_business_automation_exclusions_mode_allowed"),
        ),
        sa.CheckConstraint(
            "whatsapp_id ~ '^[1-9][0-9]{6,14}$'",
            name=op.f(
                "ck_business_automation_exclusions_whatsapp_id_normalized"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f(
                "fk_business_automation_exclusions_business_id_businesses"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_business_automation_exclusions")
        ),
        sa.UniqueConstraint(
            "business_id",
            "whatsapp_id",
            name="uq_business_automation_exclusions_business_whatsapp",
        ),
    )
    op.create_index(
        "ix_business_automation_exclusions_lookup",
        "business_automation_exclusions",
        ["business_id", "whatsapp_id", "active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_business_automation_exclusions_lookup",
        table_name="business_automation_exclusions",
    )
    op.drop_table("business_automation_exclusions")

    op.drop_index(
        "ix_conversations_automation_suppressed_until",
        table_name="conversations",
    )
    op.drop_constraint(
        op.f("ck_conversations_initiated_by_allowed"),
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_conversations_suppression_reason_allowed"),
        "conversations",
        type_="check",
    )
    for column_name in (
        "conversation_initiated_by",
        "last_human_message_at",
        "human_control_started_at",
        "suppression_reason",
        "automation_suppressed_until",
    ):
        op.drop_column("conversations", column_name)

    op.drop_constraint(
        op.f("ck_businesses_human_control_window_minutes_allowed"),
        "businesses",
        type_="check",
    )
    op.drop_column("businesses", "human_control_window_minutes")

"""Persist sanitized WhatsApp onboarding attempt event history.

Revision ID: 20261013_0033
Revises: 20261013_0032
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261013_0033"
down_revision = "20261013_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_onboarding_attempt_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("state_after", sa.String(length=32), nullable=False),
        sa.Column("authorization_code_received", sa.Boolean(), nullable=True),
        sa.Column("waba_id_received", sa.Boolean(), nullable=True),
        sa.Column("phone_number_id_received", sa.Boolean(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('client','server')",
            name="ck_whatsapp_onboarding_attempt_events_source_allowed",
        ),
        sa.CheckConstraint(
            "stage ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_whatsapp_onboarding_attempt_events_stage_sanitized",
        ),
        sa.CheckConstraint(
            "state_after ~ '^[A-Za-z0-9._-]{1,32}$'",
            name="ck_whatsapp_onboarding_attempt_events_state_after_sanitized",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_whatsapp_onboarding_attempt_events_failure_code_sanitized",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["whatsapp_onboarding_attempts.id"],
            ondelete="CASCADE",
            name="fk_wa_onboarding_event_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_whatsapp_onboarding_attempt_events_business_id_businesses",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_whatsapp_onboarding_attempt_events",
        ),
    )
    op.create_index(
        "ix_whatsapp_onboarding_attempt_events_attempt_created",
        "whatsapp_onboarding_attempt_events",
        ["attempt_id", "created_at"],
    )
    op.create_index(
        "ix_whatsapp_onboarding_attempt_events_business_created",
        "whatsapp_onboarding_attempt_events",
        ["business_id", "created_at"],
    )
    op.execute(
        'ALTER TABLE public."whatsapp_onboarding_attempt_events" ENABLE ROW LEVEL SECURITY'
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE public."whatsapp_onboarding_attempt_events" DISABLE ROW LEVEL SECURITY'
    )
    op.drop_index(
        "ix_whatsapp_onboarding_attempt_events_business_created",
        table_name="whatsapp_onboarding_attempt_events",
    )
    op.drop_index(
        "ix_whatsapp_onboarding_attempt_events_attempt_created",
        table_name="whatsapp_onboarding_attempt_events",
    )
    op.drop_table("whatsapp_onboarding_attempt_events")

"""Persist server-authoritative WhatsApp onboarding attempts.

Revision ID: 20261013_0032
Revises: 20261010_0031
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261013_0032"
down_revision = "20261010_0031"
branch_labels = None
depends_on = None


ACTIVE_STATES = (
    "'created','provider_opened','awaiting_provider',"
    "'code_received','assets_received','completing'"
)


def upgrade() -> None:
    op.create_table(
        "whatsapp_onboarding_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default="created",
            nullable=False,
        ),
        sa.Column("last_stage", sa.String(length=64), nullable=True),
        sa.Column(
            "authorization_code_received",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "waba_id_received",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "phone_number_id_received",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("empty_callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_event_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
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
            "mode IN ('coexistence', 'api_only')",
            name="ck_whatsapp_onboarding_attempts_mode_allowed",
        ),
        sa.CheckConstraint(
            "state IN ("
            "'created','provider_opened','awaiting_provider','code_received',"
            "'assets_received','completing','connected',"
            "'authorization_incomplete','cancelled','failed','expired','superseded'"
            ")",
            name="ck_whatsapp_onboarding_attempts_state_allowed",
        ),
        sa.CheckConstraint(
            "last_stage IS NULL OR last_stage ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_whatsapp_onboarding_attempts_last_stage_sanitized",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_whatsapp_onboarding_attempts_failure_code_sanitized",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_whatsapp_onboarding_attempts_business_id_businesses",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["business_whatsapp_connections.id"],
            name="fk_wa_onboarding_attempt_connection",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_whatsapp_onboarding_attempts",
        ),
    )
    op.create_index(
        "uq_whatsapp_onboarding_attempts_active_business",
        "whatsapp_onboarding_attempts",
        ["business_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({ACTIVE_STATES})"),
    )
    op.create_index(
        "uq_whatsapp_onboarding_attempts_request_id_present",
        "whatsapp_onboarding_attempts",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_whatsapp_onboarding_attempts_business_created",
        "whatsapp_onboarding_attempts",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_whatsapp_onboarding_attempts_expires_active",
        "whatsapp_onboarding_attempts",
        ["expires_at"],
        postgresql_where=sa.text(f"state IN ({ACTIVE_STATES})"),
    )
    op.execute(
        'ALTER TABLE public."whatsapp_onboarding_attempts" ENABLE ROW LEVEL SECURITY'
    )

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

    op.execute(
        'ALTER TABLE public."whatsapp_onboarding_attempts" DISABLE ROW LEVEL SECURITY'
    )
    op.drop_index(
        "ix_whatsapp_onboarding_attempts_expires_active",
        table_name="whatsapp_onboarding_attempts",
    )
    op.drop_index(
        "ix_whatsapp_onboarding_attempts_business_created",
        table_name="whatsapp_onboarding_attempts",
    )
    op.drop_index(
        "uq_whatsapp_onboarding_attempts_request_id_present",
        table_name="whatsapp_onboarding_attempts",
    )
    op.drop_index(
        "uq_whatsapp_onboarding_attempts_active_business",
        table_name="whatsapp_onboarding_attempts",
    )
    op.drop_table("whatsapp_onboarding_attempts")

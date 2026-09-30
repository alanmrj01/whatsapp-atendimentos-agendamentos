"""Add idempotent commercial automation events.

Revision ID: 20260930_0022
Revises: 20260930_0021
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260930_0022"
down_revision = "20260930_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commercial_automation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("anchor_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "event_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ('abandoned_followup_24h', 'cleaning_reminder_6m')",
            name="ck_commercial_automation_events_event_type_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'responded', 'declined', 'accepted', "
            "'handoff', 'skipped', 'failed')",
            name="ck_commercial_automation_events_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_commercial_automation_events_business_id_businesses",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_commercial_automation_events_business_customer_customers",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "conversation_id"],
            ["conversations.business_id", "conversations.id"],
            name="fk_commercial_automation_events_business_conversation_conversations",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name="fk_commercial_automation_events_business_appointment_appointments",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "result_appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name=(
                "fk_commercial_automation_events_business_result_appointment_appointments"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_commercial_automation_events_message_id_messages",
        ),
        sa.UniqueConstraint(
            "business_id",
            "event_type",
            "anchor_key",
            name="uq_commercial_automation_events_business_type_anchor",
        ),
    )
    op.create_index(
        "ix_commercial_automation_events_business_due",
        "commercial_automation_events",
        ["business_id", "due_at"],
    )
    op.create_index(
        "ix_commercial_automation_events_business_status",
        "commercial_automation_events",
        ["business_id", "status"],
    )
    op.execute(
        'ALTER TABLE public."commercial_automation_events" ENABLE ROW LEVEL SECURITY'
    )


def downgrade() -> None:
    op.drop_index(
        "ix_commercial_automation_events_business_status",
        table_name="commercial_automation_events",
    )
    op.drop_index(
        "ix_commercial_automation_events_business_due",
        table_name="commercial_automation_events",
    )
    op.drop_table("commercial_automation_events")

"""Add customer lifecycle outreach tracking.

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
        "customer_outreach",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outreach_type", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("service_label", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_appointment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "outreach_type IN ('incomplete_24h', 'cleaning_6m')",
            name="ck_customer_outreach_outreach_type_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'skipped', 'responded', 'accepted', 'declined', 'failed')",
            name="ck_customer_outreach_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_customer_outreach_business_customer_customers",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "conversation_id"],
            ["conversations.business_id", "conversations.id"],
            name="fk_customer_outreach_business_conversation_conversations",
        ),
        sa.ForeignKeyConstraint(
            ["source_appointment_id"],
            ["appointments.id"],
            name="fk_customer_outreach_source_appointment_id_appointments",
        ),
        sa.ForeignKeyConstraint(
            ["result_appointment_id"],
            ["appointments.id"],
            name="fk_customer_outreach_result_appointment_id_appointments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customer_outreach"),
    )
    op.create_index(
        "ix_customer_outreach_business_due",
        "customer_outreach",
        ["business_id", "due_at"],
    )
    op.create_index(
        "ix_customer_outreach_conversation",
        "customer_outreach",
        ["conversation_id"],
    )
    op.create_index(
        "ix_customer_outreach_status",
        "customer_outreach",
        ["status"],
    )
    op.create_index(
        "uq_customer_outreach_idempotency_key",
        "customer_outreach",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_customer_outreach_idempotency_key", table_name="customer_outreach")
    op.drop_index("ix_customer_outreach_status", table_name="customer_outreach")
    op.drop_index("ix_customer_outreach_conversation", table_name="customer_outreach")
    op.drop_index("ix_customer_outreach_business_due", table_name="customer_outreach")
    op.drop_table("customer_outreach")

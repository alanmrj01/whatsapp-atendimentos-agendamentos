"""Add persistent in-app notifications for automatic bookings.

Revision ID: 20260922_0015
Revises: 20260922_0014
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260922_0015"
down_revision = "20260922_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_appointments_business_id_id",
        "appointments",
        ["business_id", "id"],
    )
    op.create_table(
        "business_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
            server_default="automatic_booking_confirmed",
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type = 'automatic_booking_confirmed'",
            name=op.f("ck_business_notifications_event_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_business_notifications_business_id_businesses"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name=(
                "fk_business_notifications_business_appointment_appointments"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_notifications")),
        sa.UniqueConstraint(
            "business_id",
            "appointment_id",
            "event_type",
            name="uq_business_notifications_appointment_event",
        ),
    )
    op.create_index(
        "ix_business_notifications_business_created_at",
        "business_notifications",
        ["business_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_business_notifications_business_read_at",
        "business_notifications",
        ["business_id", "read_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_business_notifications_business_read_at",
        table_name="business_notifications",
    )
    op.drop_index(
        "ix_business_notifications_business_created_at",
        table_name="business_notifications",
    )
    op.drop_table("business_notifications")
    op.drop_constraint(
        "uq_appointments_business_id_id",
        "appointments",
        type_="unique",
    )

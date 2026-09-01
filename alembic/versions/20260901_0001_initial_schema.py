"""Create the initial scheduling schema.

Revision ID: 20260901_0001
Revises:
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'America/Sao_Paulo'"),
            nullable=False,
        ),
        sa.Column("meta_phone_number_id", sa.String(length=255), nullable=True),
        sa.Column("meta_waba_id", sa.String(length=255), nullable=True),
        sa.Column(
            "slot_interval_minutes",
            sa.Integer(),
            server_default=sa.text("30"),
            nullable=False,
        ),
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
            "slot_interval_minutes > 0",
            name=op.f("ck_businesses_slot_interval_minutes_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_businesses"),
        sa.UniqueConstraint(
            "meta_phone_number_id",
            name="uq_businesses_meta_phone_number_id",
        ),
    )
    op.create_index("ix_businesses_active", "businesses", ["active"])

    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("whatsapp_id", sa.String(length=255), nullable=False),
        sa.Column("phone_e164", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_customers_business_id_businesses",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint(
            "business_id",
            "whatsapp_id",
            name="uq_customers_business_whatsapp",
        ),
        sa.UniqueConstraint(
            "business_id",
            "id",
            name="uq_customers_business_id_id",
        ),
    )
    op.create_index("ix_customers_business_id", "customers", ["business_id"])

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "automation_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "handoff_status",
            sa.String(length=32),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column(
            "last_interaction_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_conversations_business_customer_customers",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint(
            "business_id",
            "customer_id",
            name="uq_conversations_business_customer",
        ),
        sa.UniqueConstraint(
            "business_id",
            "id",
            name="uq_conversations_business_id_id",
        ),
    )
    op.create_index(
        "ix_conversations_business_id", "conversations", ["business_id"]
    )
    op.create_index(
        "ix_conversations_customer_id", "conversations", ["customer_id"]
    )
    op.create_index("ix_conversations_state", "conversations", ["state"])
    op.create_index(
        "ix_conversations_handoff_status", "conversations", ["handoff_status"]
    )
    op.create_index(
        "ix_conversations_last_interaction_at",
        "conversations",
        ["last_interaction_at"],
    )

    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
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
            "duration_minutes > 0",
            name=op.f("ck_services_duration_minutes_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_services_business_id_businesses",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.UniqueConstraint(
            "business_id", "id", name="uq_services_business_id_id"
        ),
    )
    op.create_index("ix_services_business_id", "services", ["business_id"])
    op.create_index("ix_services_active", "services", ["active"])

    op.create_table(
        "employees",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_employees_business_id_businesses",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employees"),
        sa.UniqueConstraint(
            "business_id", "id", name="uq_employees_business_id_id"
        ),
    )
    op.create_index("ix_employees_business_id", "employees", ["business_id"])
    op.create_index("ix_employees_active", "employees", ["active"])

    op.create_table(
        "employee_services",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_employee_services_business_id_businesses",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_employee_services_business_employee_employees",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_employee_services_business_service_services",
        ),
        sa.PrimaryKeyConstraint(
            "employee_id", "service_id", name="pk_employee_services"
        ),
    )
    op.create_index(
        "ix_employee_services_service_id", "employee_services", ["service_id"]
    )
    op.create_index(
        "ix_employee_services_business_id", "employee_services", ["business_id"]
    )

    op.create_table(
        "working_hours",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.CheckConstraint(
            "weekday BETWEEN 0 AND 6",
            name=op.f("ck_working_hours_weekday_range"),
        ),
        sa.CheckConstraint(
            "end_time > start_time",
            name=op.f("ck_working_hours_end_time_after_start_time"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_working_hours_business_employee_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_working_hours"),
    )
    op.create_index(
        "ix_working_hours_business_id", "working_hours", ["business_id"]
    )
    op.create_index(
        "ix_working_hours_employee_weekday",
        "working_hours",
        ["employee_id", "weekday"],
    )

    op.create_table(
        "schedule_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name=op.f("ck_schedule_blocks_ends_at_after_starts_at"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_schedule_blocks_business_employee_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_blocks"),
    )
    op.create_index(
        "ix_schedule_blocks_business_id", "schedule_blocks", ["business_id"]
    )
    op.create_index(
        "ix_schedule_blocks_employee_starts_at",
        "schedule_blocks",
        ["employee_id", "starts_at"],
    )

    op.create_table(
        "appointments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
            "ends_at > starts_at",
            name=op.f("ck_appointments_ends_at_after_starts_at"),
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed')",
            name=op.f("ck_appointments_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_appointments_business_customer_customers",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_appointments_business_service_services",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_appointments_business_employee_employees",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_appointments"),
    )
    op.create_index(
        "ix_appointments_business_id", "appointments", ["business_id"]
    )
    op.create_index(
        "ix_appointments_customer_id", "appointments", ["customer_id"]
    )
    op.create_index(
        "ix_appointments_service_id", "appointments", ["service_id"]
    )
    op.create_index(
        "ix_appointments_employee_starts_at",
        "appointments",
        ["employee_id", "starts_at"],
    )
    op.create_index("ix_appointments_status", "appointments", ["status"])
    op.create_index("ix_appointments_starts_at", "appointments", ["starts_at"])
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT excl_appointments_employee_confirmed_overlap
        EXCLUDE USING gist (
            employee_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        ) WHERE (status = 'confirmed')
        """
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("interactive_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
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
            "direction IN ('inbound', 'outbound')",
            name=op.f("ck_messages_direction_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "conversation_id"],
            ["conversations.business_id", "conversations.id"],
            name="fk_messages_business_conversation_conversations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )
    op.create_index("ix_messages_business_id", "messages", ["business_id"])
    op.create_index(
        "ix_messages_conversation_id", "messages", ["conversation_id"]
    )
    op.create_index("ix_messages_status", "messages", ["status"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index(
        "uq_messages_provider_message_id_present",
        "messages",
        ["provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_messages_idempotency_key_present",
        "messages",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "processed_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_processed_webhooks"),
        sa.UniqueConstraint(
            "event_key", name="uq_processed_webhooks_event_key"
        ),
    )
    op.create_index(
        "ix_processed_webhooks_provider_message_id",
        "processed_webhooks",
        ["provider_message_id"],
    )
    op.create_index(
        "ix_processed_webhooks_status", "processed_webhooks", ["status"]
    )
    op.create_index(
        "ix_processed_webhooks_received_at",
        "processed_webhooks",
        ["received_at"],
    )


def downgrade() -> None:
    # btree_gist may predate this application or be shared by another schema.
    op.drop_table("processed_webhooks")
    op.drop_table("messages")
    op.drop_constraint(
        "excl_appointments_employee_confirmed_overlap", "appointments"
    )
    op.drop_table("appointments")
    op.drop_table("schedule_blocks")
    op.drop_table("working_hours")
    op.drop_table("employee_services")
    op.drop_table("employees")
    op.drop_table("services")
    op.drop_table("conversations")
    op.drop_table("customers")
    op.drop_table("businesses")

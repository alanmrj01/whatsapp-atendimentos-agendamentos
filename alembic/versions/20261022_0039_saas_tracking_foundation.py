"""Strengthen SaaS tracking and recurring billing observability.

Revision ID: 20261022_0039
Revises: 20261022_0038
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261022_0039"
down_revision = "20261022_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marketing_funnel_events",
        sa.Column("client_event_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "marketing_funnel_events",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE marketing_funnel_events "
        "SET occurred_at = created_at "
        "WHERE occurred_at IS NULL"
    )
    op.alter_column(
        "marketing_funnel_events",
        "occurred_at",
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.create_index(
        "uq_marketing_funnel_events_client_event_id",
        "marketing_funnel_events",
        ["client_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_marketing_funnel_events_event_occurred",
        "marketing_funnel_events",
        ["event_name", "occurred_at"],
    )
    op.create_index(
        "ix_marketing_funnel_events_properties_gin",
        "marketing_funnel_events",
        ["properties"],
        postgresql_using="gin",
    )

    op.add_column(
        "billing_webhook_events",
        sa.Column(
            "provider_environment",
            sa.String(length=16),
            server_default=sa.text("'production'"),
            nullable=False,
        ),
    )
    op.add_column(
        "billing_webhook_events",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "billing_webhook_events",
        sa.Column("amount_cents", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "billing_webhook_event_provider_environment_allowed",
        "billing_webhook_events",
        "provider_environment IN ('sandbox', 'production')",
    )
    op.create_check_constraint(
        "billing_webhook_event_amount_positive",
        "billing_webhook_events",
        "amount_cents IS NULL OR amount_cents > 0",
    )
    op.execute(
        "UPDATE billing_webhook_events "
        "SET provider_payment_id = resource_id "
        "WHERE resource_type = 'payment' "
        "AND provider_payment_id IS NULL"
    )
    op.create_index(
        "ix_billing_webhook_events_business_received",
        "billing_webhook_events",
        ["business_id", "received_at"],
    )
    op.create_index(
        "ix_billing_webhook_events_event_type_received",
        "billing_webhook_events",
        ["event_type", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_webhook_events_event_type_received",
        table_name="billing_webhook_events",
    )
    op.drop_index(
        "ix_billing_webhook_events_business_received",
        table_name="billing_webhook_events",
    )
    op.drop_constraint(
        "billing_webhook_event_amount_positive",
        "billing_webhook_events",
        type_="check",
    )
    op.drop_constraint(
        "billing_webhook_event_provider_environment_allowed",
        "billing_webhook_events",
        type_="check",
    )
    op.drop_column("billing_webhook_events", "amount_cents")
    op.drop_column("billing_webhook_events", "business_id")
    op.drop_column("billing_webhook_events", "provider_environment")

    op.drop_index(
        "ix_marketing_funnel_events_properties_gin",
        table_name="marketing_funnel_events",
    )
    op.drop_index(
        "ix_marketing_funnel_events_event_occurred",
        table_name="marketing_funnel_events",
    )
    op.drop_index(
        "uq_marketing_funnel_events_client_event_id",
        table_name="marketing_funnel_events",
    )
    op.drop_column("marketing_funnel_events", "occurred_at")
    op.drop_column("marketing_funnel_events", "client_event_id")

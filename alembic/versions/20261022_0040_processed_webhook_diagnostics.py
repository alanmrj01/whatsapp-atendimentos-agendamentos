"""Add operational diagnostic outcomes to processed webhooks.

Revision ID: 20261022_0040
Revises: 20261022_0039
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261022_0040"
down_revision = "20261022_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processed_webhooks",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "processed_webhooks",
        sa.Column("outcome_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "processed_webhooks",
        sa.Column("provider_failure_code", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_processed_webhooks_business_id_businesses",
        "processed_webhooks",
        "businesses",
        ["business_id"],
        ["id"],
    )
    op.create_check_constraint(
        "processed_webhook_outcome_reason_sanitized",
        "processed_webhooks",
        "outcome_reason IS NULL OR outcome_reason ~ '^[a-z0-9_.-]{1,64}$'",
    )
    op.create_check_constraint(
        "processed_webhook_provider_failure_code_sanitized",
        "processed_webhooks",
        "provider_failure_code IS NULL OR "
        "provider_failure_code ~ '^[A-Za-z0-9._-]{1,64}$'",
    )
    op.create_index(
        "ix_processed_webhooks_business_received",
        "processed_webhooks",
        ["business_id", "received_at"],
    )
    op.create_index(
        "ix_processed_webhooks_outcome_received",
        "processed_webhooks",
        ["outcome_reason", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processed_webhooks_outcome_received",
        table_name="processed_webhooks",
    )
    op.drop_index(
        "ix_processed_webhooks_business_received",
        table_name="processed_webhooks",
    )
    op.drop_constraint(
        "processed_webhook_provider_failure_code_sanitized",
        "processed_webhooks",
        type_="check",
    )
    op.drop_constraint(
        "processed_webhook_outcome_reason_sanitized",
        "processed_webhooks",
        type_="check",
    )
    op.drop_constraint(
        "fk_processed_webhooks_business_id_businesses",
        "processed_webhooks",
        type_="foreignkey",
    )
    op.drop_column("processed_webhooks", "provider_failure_code")
    op.drop_column("processed_webhooks", "outcome_reason")
    op.drop_column("processed_webhooks", "business_id")

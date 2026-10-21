"""Add safe guarantee refund state and initial payment correlation.

Revision ID: 20261021_0037
Revises: 20261020_0036
"""

from alembic import op
import sqlalchemy as sa


revision = "20261021_0037"
down_revision = "20261020_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_checkouts",
        sa.Column("provider_payment_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "uq_billing_checkouts_env_provider_payment_id",
        "billing_checkouts",
        ["provider_environment", "provider_payment_id"],
        unique=True,
    )

    op.add_column(
        "commercial_subscriptions",
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "commercial_subscriptions",
        sa.Column(
            "refund_status",
            sa.String(length=24),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "commercial_subscriptions",
        sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "commercial_subscriptions",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "commercial_subscription_refund_status_allowed",
        "commercial_subscriptions",
        "refund_status IN ('none', 'requested', 'reconcile_required', 'refunded')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "commercial_subscription_refund_status_allowed",
        "commercial_subscriptions",
        type_="check",
    )
    op.drop_column("commercial_subscriptions", "refunded_at")
    op.drop_column("commercial_subscriptions", "refund_requested_at")
    op.drop_column("commercial_subscriptions", "cancellation_requested_at")
    op.drop_column("commercial_subscriptions", "refund_status")
    op.drop_index(
        "uq_billing_checkouts_env_provider_payment_id",
        table_name="billing_checkouts",
    )
    op.drop_column("billing_checkouts", "provider_payment_id")

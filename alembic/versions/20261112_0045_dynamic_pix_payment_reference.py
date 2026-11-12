"""Add provider payment reference to commercial subscriptions.

Revision ID: 20261112_0045
Revises: 20261111_0044
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0045"
down_revision = "20261111_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commercial_subscriptions",
        sa.Column("provider_payment_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "uq_commercial_subscriptions_env_provider_payment_id",
        "commercial_subscriptions",
        ["provider_environment", "provider_payment_id"],
        unique=True,
    )
    op.drop_constraint(
        "commercial_subscription_provider_reference_required",
        "commercial_subscriptions",
        type_="check",
    )
    op.create_check_constraint(
        "commercial_subscription_provider_reference_required",
        "commercial_subscriptions",
        (
            "provider_subscription_id IS NOT NULL OR "
            "provider_authorization_id IS NOT NULL OR "
            "provider_pix_qr_code_id IS NOT NULL OR "
            "provider_payment_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE commercial_subscriptions
        SET provider_pix_qr_code_id = provider_payment_id
        WHERE provider_payment_id IS NOT NULL
          AND provider_subscription_id IS NULL
          AND provider_authorization_id IS NULL
          AND provider_pix_qr_code_id IS NULL
        """
    )
    op.drop_constraint(
        "commercial_subscription_provider_reference_required",
        "commercial_subscriptions",
        type_="check",
    )
    op.create_check_constraint(
        "commercial_subscription_provider_reference_required",
        "commercial_subscriptions",
        (
            "provider_subscription_id IS NOT NULL OR "
            "provider_authorization_id IS NOT NULL OR "
            "provider_pix_qr_code_id IS NOT NULL"
        ),
    )
    op.drop_index(
        "uq_commercial_subscriptions_env_provider_payment_id",
        table_name="commercial_subscriptions",
    )
    op.drop_column("commercial_subscriptions", "provider_payment_id")

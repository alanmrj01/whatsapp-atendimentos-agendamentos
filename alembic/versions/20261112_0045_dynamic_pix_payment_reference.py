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
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            DROP CONSTRAINT IF EXISTS commercial_subscription_provider_reference_required
        """
    )
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            ADD CONSTRAINT commercial_subscription_provider_reference_required
            CHECK (
                provider_subscription_id IS NOT NULL
                OR provider_authorization_id IS NOT NULL
                OR provider_pix_qr_code_id IS NOT NULL
                OR provider_payment_id IS NOT NULL
            )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    dynamic_rows = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM commercial_subscriptions
            WHERE provider_payment_id IS NOT NULL
            LIMIT 1
            """
        )
    ).first()
    if dynamic_rows is not None:
        raise RuntimeError(
            "Cannot drop provider_payment_id while dynamic Pix subscriptions exist"
        )

    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            DROP CONSTRAINT IF EXISTS commercial_subscription_provider_reference_required
        """
    )
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            ADD CONSTRAINT commercial_subscription_provider_reference_required
            CHECK (
                provider_subscription_id IS NOT NULL
                OR provider_authorization_id IS NOT NULL
                OR provider_pix_qr_code_id IS NOT NULL
            )
        """
    )
    op.drop_index(
        "uq_commercial_subscriptions_env_provider_payment_id",
        table_name="commercial_subscriptions",
    )
    op.drop_column("commercial_subscriptions", "provider_payment_id")

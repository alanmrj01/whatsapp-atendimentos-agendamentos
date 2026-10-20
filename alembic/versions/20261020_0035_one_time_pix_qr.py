"""Add provider references for one-time Pix QR checkouts.

Revision ID: 20261020_0035
Revises: 20261019_0034
"""

from alembic import op
import sqlalchemy as sa


revision = "20261020_0035"
down_revision = "20261019_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_checkouts",
        sa.Column("provider_pix_qr_code_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_billing_checkouts_provider_pix_qr_code_id",
        "billing_checkouts",
        ["provider_pix_qr_code_id"],
        unique=True,
    )

    op.add_column(
        "commercial_subscriptions",
        sa.Column("provider_pix_qr_code_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_commercial_subscriptions_provider_pix_qr_code_id",
        "commercial_subscriptions",
        ["provider_pix_qr_code_id"],
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
            )
        """
    )


def downgrade() -> None:
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
            )
        """
    )
    op.drop_index(
        "ix_commercial_subscriptions_provider_pix_qr_code_id",
        table_name="commercial_subscriptions",
    )
    op.drop_column("commercial_subscriptions", "provider_pix_qr_code_id")
    op.drop_index(
        "ix_billing_checkouts_provider_pix_qr_code_id",
        table_name="billing_checkouts",
    )
    op.drop_column("billing_checkouts", "provider_pix_qr_code_id")

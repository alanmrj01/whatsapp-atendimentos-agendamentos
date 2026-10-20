"""Remove legacy commercial subscription constraints blocking one-time Pix.

Revision ID: 20261020_0036
Revises: 20261020_0035
"""

from alembic import op


revision = "20261020_0036"
down_revision = "20261020_0035"
branch_labels = None
depends_on = None


LEGACY_PAYMENT_METHOD_CONSTRAINT = (
    "ck_commercial_subscriptions_commercial_subscription_pay_61f2"
)
LEGACY_PROVIDER_REFERENCE_CONSTRAINT = (
    "ck_commercial_subscriptions_commercial_subscription_pro_23df"
)


def upgrade() -> None:
    # These constraints were created by an older migration from the then-current
    # ORM model. Migration 0035 added the new permissive named constraints but
    # could not replace these differently named legacy checks. Keeping both
    # checks makes plain Pix subscriptions impossible.
    op.execute(
        f"""
        ALTER TABLE commercial_subscriptions
            DROP CONSTRAINT IF EXISTS {LEGACY_PAYMENT_METHOD_CONSTRAINT}
        """
    )
    op.execute(
        f"""
        ALTER TABLE commercial_subscriptions
            DROP CONSTRAINT IF EXISTS {LEGACY_PROVIDER_REFERENCE_CONSTRAINT}
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE commercial_subscriptions
            ADD CONSTRAINT {LEGACY_PAYMENT_METHOD_CONSTRAINT}
            CHECK (payment_method IN ('credit_card', 'pix_automatic'))
        """
    )
    op.execute(
        f"""
        ALTER TABLE commercial_subscriptions
            ADD CONSTRAINT {LEGACY_PROVIDER_REFERENCE_CONSTRAINT}
            CHECK (
                provider_subscription_id IS NOT NULL
                OR provider_authorization_id IS NOT NULL
            )
        """
    )

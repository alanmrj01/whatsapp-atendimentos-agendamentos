"""Reconcile commercial billing schema with the current runtime models.

Revision ID: 20261003_0028
Revises: 20261002_0027

This migration is intentionally idempotent. Some production databases already
contain these columns/indexes from the staged billing rollout, while a database
created strictly from the historical Alembic chain does not. The migration
closes that gap without rewriting existing production data.
"""

from alembic import op


revision = "20261003_0028"
down_revision = "20261002_0027"
branch_labels = None
depends_on = None


def _add_constraint_if_missing(table: str, name: str, expression: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = '{name}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table}
                ADD CONSTRAINT {name} CHECK ({expression});
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE billing_checkouts
            ADD COLUMN IF NOT EXISTS payment_method varchar(24),
            ADD COLUMN IF NOT EXISTS provider_environment varchar(16),
            ADD COLUMN IF NOT EXISTS provider_authorization_id varchar(100),
            ADD COLUMN IF NOT EXISTS pix_conciliation_identifier varchar(100),
            ADD COLUMN IF NOT EXISTS pix_qr_payload text,
            ADD COLUMN IF NOT EXISTS pix_qr_expires_at timestamptz
        """
    )
    op.execute(
        """
        UPDATE billing_checkouts
        SET payment_method = 'credit_card'
        WHERE payment_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE billing_checkouts
        SET provider_environment = 'production'
        WHERE provider_environment IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE billing_checkouts
            ALTER COLUMN payment_method SET NOT NULL,
            ALTER COLUMN provider_environment SET NOT NULL
        """
    )
    _add_constraint_if_missing(
        "billing_checkouts",
        "billing_checkout_payment_method_allowed",
        "payment_method IN ('credit_card', 'pix_automatic')",
    )
    _add_constraint_if_missing(
        "billing_checkouts",
        "billing_checkout_provider_environment_allowed",
        "provider_environment IN ('sandbox', 'production')",
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            ix_billing_checkouts_provider_authorization_id
        ON billing_checkouts (provider_authorization_id);
        """
    )

    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            ADD COLUMN IF NOT EXISTS payment_method varchar(24),
            ADD COLUMN IF NOT EXISTS provider_environment varchar(16),
            ADD COLUMN IF NOT EXISTS provider_authorization_id varchar(100)
        """
    )
    op.execute(
        """
        UPDATE commercial_subscriptions
        SET payment_method = 'credit_card'
        WHERE payment_method IS NULL
        """
    )
    op.execute(
        """
        UPDATE commercial_subscriptions
        SET provider_environment = 'production'
        WHERE provider_environment IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            ALTER COLUMN payment_method SET NOT NULL,
            ALTER COLUMN provider_environment SET NOT NULL,
            ALTER COLUMN provider_subscription_id DROP NOT NULL
        """
    )
    _add_constraint_if_missing(
        "commercial_subscriptions",
        "commercial_subscription_payment_method_allowed",
        "payment_method IN ('credit_card', 'pix_automatic')",
    )
    _add_constraint_if_missing(
        "commercial_subscriptions",
        "commercial_subscription_provider_environment_allowed",
        "provider_environment IN ('sandbox', 'production')",
    )
    _add_constraint_if_missing(
        "commercial_subscriptions",
        "commercial_subscription_provider_reference_required",
        "provider_subscription_id IS NOT NULL OR provider_authorization_id IS NOT NULL",
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            ix_commercial_subscriptions_provider_authorization_id
        ON commercial_subscriptions (provider_authorization_id);
        """
    )

    op.execute(
        """
        ALTER TABLE billing_webhook_events
            ADD COLUMN IF NOT EXISTS provider_payment_id varchar(100),
            ADD COLUMN IF NOT EXISTS provider_authorization_id varchar(100)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
            ix_billing_webhook_events_provider_payment_id
        ON billing_webhook_events (provider_payment_id)
        """
    )


def downgrade() -> None:
    # Intentionally non-destructive: this revision reconciles databases that may
    # have received the billing columns before Alembic tracked them. Dropping
    # those fields on downgrade could destroy live billing references.
    pass

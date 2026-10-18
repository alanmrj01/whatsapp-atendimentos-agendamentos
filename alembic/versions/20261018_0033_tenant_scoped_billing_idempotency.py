"""Scope billing checkout idempotency to business and provider environment.

Revision ID: 20261018_0033
Revises: 20261013_0032
"""

from alembic import op


revision = "20261018_0033"
down_revision = "20261013_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical schema created idempotency_key as globally unique. That couples
    # unrelated tenants. Drop whichever single-column unique constraint exists,
    # then enforce uniqueness only inside a business/provider environment.
    op.execute(
        """
        DO $$
        DECLARE
            constraint_name text;
        BEGIN
            SELECT c.conname
            INTO constraint_name
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'billing_checkouts'
              AND c.contype = 'u'
              AND (
                  SELECT array_agg(a.attname ORDER BY u.ordinality)
                  FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ordinality)
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid
                   AND a.attnum = u.attnum
              ) = ARRAY['idempotency_key']::name[]
            LIMIT 1;

            IF constraint_name IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE billing_checkouts DROP CONSTRAINT %I',
                    constraint_name
                );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_billing_checkouts_business_env_idempotency
        ON billing_checkouts (
            business_id,
            provider_environment,
            idempotency_key
        )
        """
    )

    op.execute(
        """
        ALTER TABLE billing_checkouts
            DROP CONSTRAINT IF EXISTS billing_checkout_payment_method_allowed
        """
    )
    op.execute(
        """
        ALTER TABLE billing_checkouts
            ADD CONSTRAINT billing_checkout_payment_method_allowed
            CHECK (payment_method IN ('credit_card', 'pix', 'pix_automatic'))
        """
    )
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            DROP CONSTRAINT IF EXISTS commercial_subscription_payment_method_allowed
        """
    )
    op.execute(
        """
        ALTER TABLE commercial_subscriptions
            ADD CONSTRAINT commercial_subscription_payment_method_allowed
            CHECK (payment_method IN ('credit_card', 'pix', 'pix_automatic'))
        """
    )


def downgrade() -> None:
    # Restoring global uniqueness could fail after two businesses legitimately
    # reuse the same UUID, so keep downgrade non-destructive.
    op.execute(
        """
        DROP INDEX IF EXISTS uq_billing_checkouts_business_env_idempotency
        """
    )

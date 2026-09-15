"""Add commercial billing tables without changing administrative entitlements.

Revision ID: 20260915_0010
Revises: 20260915_0009
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260915_0010"
down_revision = "20260915_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_checkouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False),
        sa.Column("payment_method", sa.String(length=24), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'creating'"), nullable=False),
        sa.Column("provider_checkout_id", sa.String(length=80), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("provider_authorization_id", sa.String(length=100), nullable=True),
        sa.Column("pix_conciliation_identifier", sa.String(length=100), nullable=True),
        sa.Column("pix_qr_payload", sa.Text(), nullable=True),
        sa.Column("pix_qr_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_customer_id", sa.String(length=80), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("plan_code IN ('basic', 'plus')", name="billing_checkout_plan_allowed"),
        sa.CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'annual')",
            name="billing_checkout_cycle_allowed",
        ),
        sa.CheckConstraint(
            "payment_method IN ('credit_card', 'pix_automatic')",
            name="billing_checkout_payment_method_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('creating', 'active', 'paid', 'canceled', 'expired', 'failed')",
            name="billing_checkout_status_allowed",
        ),
        sa.CheckConstraint("amount_cents > 0", name="billing_checkout_amount_positive"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_billing_checkouts_business_id", "billing_checkouts", ["business_id"])
    op.create_index(
        "ix_billing_checkouts_provider_checkout_id",
        "billing_checkouts",
        ["provider_checkout_id"],
        unique=True,
    )
    op.create_index(
        "ix_billing_checkouts_provider_authorization_id",
        "billing_checkouts",
        ["provider_authorization_id"],
        unique=True,
    )

    op.create_table(
        "commercial_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("billing_cycle", sa.String(length=16), nullable=False),
        sa.Column("payment_method", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_subscription_id", sa.String(length=80), nullable=True),
        sa.Column("provider_authorization_id", sa.String(length=100), nullable=True),
        sa.Column("provider_customer_id", sa.String(length=80), nullable=True),
        sa.Column("access_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("plan_code IN ('basic', 'plus')", name="commercial_subscription_plan_allowed"),
        sa.CheckConstraint(
            "billing_cycle IN ('monthly', 'quarterly', 'annual')",
            name="commercial_subscription_cycle_allowed",
        ),
        sa.CheckConstraint(
            "payment_method IN ('credit_card', 'pix_automatic')",
            name="commercial_subscription_payment_method_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'suspended')",
            name="commercial_subscription_status_allowed",
        ),
        sa.CheckConstraint(
            "provider_subscription_id IS NOT NULL OR provider_authorization_id IS NOT NULL",
            name="commercial_subscription_provider_reference_required",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(["checkout_id"], ["billing_checkouts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id"),
    )
    op.create_index(
        "ix_commercial_subscriptions_business_id",
        "commercial_subscriptions",
        ["business_id"],
    )
    op.create_index(
        "ix_commercial_subscriptions_provider_subscription_id",
        "commercial_subscriptions",
        ["provider_subscription_id"],
        unique=True,
    )
    op.create_index(
        "ix_commercial_subscriptions_provider_authorization_id",
        "commercial_subscriptions",
        ["provider_authorization_id"],
        unique=True,
    )

    op.create_table(
        "billing_webhook_events",
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=100), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("billing_webhook_events")
    op.drop_index(
        "ix_commercial_subscriptions_provider_authorization_id",
        table_name="commercial_subscriptions",
    )
    op.drop_index(
        "ix_commercial_subscriptions_provider_subscription_id",
        table_name="commercial_subscriptions",
    )
    op.drop_index("ix_commercial_subscriptions_business_id", table_name="commercial_subscriptions")
    op.drop_table("commercial_subscriptions")
    op.drop_index("ix_billing_checkouts_provider_authorization_id", table_name="billing_checkouts")
    op.drop_index("ix_billing_checkouts_provider_checkout_id", table_name="billing_checkouts")
    op.drop_index("ix_billing_checkouts_business_id", table_name="billing_checkouts")
    op.drop_table("billing_checkouts")

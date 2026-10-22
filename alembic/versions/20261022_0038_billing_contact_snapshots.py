"""Persist billing contact snapshots for refund retention operations.

Revision ID: 20261022_0038
Revises: 20261021_0037
"""

from alembic import op
import sqlalchemy as sa


revision = "20261022_0038"
down_revision = "20261021_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_checkouts",
        sa.Column("payer_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "billing_checkouts",
        sa.Column("payer_email", sa.String(length=254), nullable=True),
    )
    op.add_column(
        "billing_checkouts",
        sa.Column("payer_phone", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("billing_checkouts", "payer_phone")
    op.drop_column("billing_checkouts", "payer_email")
    op.drop_column("billing_checkouts", "payer_name")

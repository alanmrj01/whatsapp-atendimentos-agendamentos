"""Track trusted customer name provenance.

Revision ID: 20260930_0024
Revises: 20260930_0023
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0024"
down_revision = "20260930_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("name_source", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_customers_name_source_allowed",
        "customers",
        "name_source IS NULL OR name_source IN ('manual', 'conversation')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_customers_name_source_allowed",
        "customers",
        type_="check",
    )
    op.drop_column("customers", "name_source")

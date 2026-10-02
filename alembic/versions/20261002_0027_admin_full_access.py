"""Add explicit administrative unlimited access entitlement.

Revision ID: 20261002_0027
Revises: 20261001_0026
"""

from alembic import op
import sqlalchemy as sa


revision = "20261002_0027"
down_revision = "20261001_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical access_mode='paid' is intentionally NOT promoted here.
    # Only an explicit platform-admin grant may set admin_full_access=true.
    op.add_column(
        "business_access",
        sa.Column(
            "admin_full_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("business_access", "admin_full_access")

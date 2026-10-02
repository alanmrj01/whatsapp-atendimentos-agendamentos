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
    op.add_column(
        "business_access",
        sa.Column(
            "admin_full_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Before commercial billing went live, an explicit paid access row represented
    # administrative/pilot access. Preserve those tenants as unlimited overrides.
    op.execute(
        """
        UPDATE business_access
        SET admin_full_access = true,
            has_had_operational_access = true
        WHERE access_mode = 'paid'
        """
    )

    # Admin-created legacy tenants can have no row because missing rows were
    # historically interpreted as paid. Materialize them explicitly.
    op.execute(
        """
        INSERT INTO business_access (
            business_id,
            access_mode,
            has_had_operational_access,
            admin_full_access
        )
        SELECT
            b.id,
            'paid',
            true,
            true
        FROM businesses AS b
        LEFT JOIN business_access AS ba
          ON ba.business_id = b.id
        WHERE ba.business_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("business_access", "admin_full_access")

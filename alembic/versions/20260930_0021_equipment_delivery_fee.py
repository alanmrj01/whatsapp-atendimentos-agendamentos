"""Add equipment delivery fee per kilometer.

Revision ID: 20260930_0021
Revises: 20260928_0020
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0021"
down_revision = "20260928_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "equipment_delivery_fee_per_km",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="2.40",
        ),
    )
    op.create_check_constraint(
        "ck_businesses_equipment_delivery_fee_per_km_nonnegative",
        "businesses",
        "equipment_delivery_fee_per_km >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_businesses_equipment_delivery_fee_per_km_nonnegative",
        "businesses",
        type_="check",
    )
    op.drop_column("businesses", "equipment_delivery_fee_per_km")

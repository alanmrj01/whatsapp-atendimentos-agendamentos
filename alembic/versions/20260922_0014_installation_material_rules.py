"""Add HVAC installation material rules.

Revision ID: 20260922_0014
Revises: 20260922_0013
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa

revision = "20260922_0014"
down_revision = "20260922_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "services",
        sa.Column("asks_tubing_length", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "services",
        sa.Column("included_tubing_meters", sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("tubing_meters", sa.Numeric(8, 2), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_appointments_tubing_meters_positive"),
        "appointments",
        "tubing_meters IS NULL OR tubing_meters > 0",
    )
    op.create_check_constraint(
        op.f("ck_services_included_tubing_meters_nonnegative"),
        "services",
        "included_tubing_meters IS NULL OR included_tubing_meters >= 0",
    )
    op.execute(
        """
        UPDATE services
        SET asks_tubing_length = true,
            included_tubing_meters = COALESCE(included_tubing_meters, 3)
        WHERE lower(name) LIKE '%instala%'
        """
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_appointments_tubing_meters_positive"), "appointments", type_="check")
    op.drop_column("appointments", "tubing_meters")
    op.drop_constraint(
        op.f("ck_services_included_tubing_meters_nonnegative"),
        "services",
        type_="check",
    )
    op.drop_column("services", "included_tubing_meters")
    op.drop_column("services", "asks_tubing_length")

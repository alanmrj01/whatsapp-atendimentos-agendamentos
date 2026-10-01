"""Add service distance settings and backfill technician specialties.

Revision ID: 20261001_0026
Revises: 20261001_0025
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "20261001_0026"
down_revision = "20261001_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("service_radius_km", sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "service_distance_included_km",
            sa.Numeric(8, 2),
            server_default="15.00",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "service_distance_fee_per_km",
            sa.Numeric(10, 2),
            server_default="2.40",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "service_radius_km_nonnegative",
        "businesses",
        "service_radius_km IS NULL OR service_radius_km >= 0",
    )
    op.create_check_constraint(
        "service_distance_included_km_nonnegative",
        "businesses",
        "service_distance_included_km >= 0",
    )
    op.create_check_constraint(
        "service_distance_fee_per_km_nonnegative",
        "businesses",
        "service_distance_fee_per_km >= 0",
    )

    # The specialty matrix already exists. Existing MVP technicians historically
    # meant "can do every active service", so make that implicit rule explicit.
    op.execute(
        """
        INSERT INTO employee_services (business_id, employee_id, service_id)
        SELECT e.business_id, e.id, s.id
        FROM employees AS e
        JOIN services AS s
          ON s.business_id = e.business_id
         AND s.active IS TRUE
        WHERE e.active IS TRUE
          AND e.operational_role = 'technician'
        ON CONFLICT (employee_id, service_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "service_distance_fee_per_km_nonnegative",
        "businesses",
        type_="check",
    )
    op.drop_constraint(
        "service_distance_included_km_nonnegative",
        "businesses",
        type_="check",
    )
    op.drop_constraint(
        "service_radius_km_nonnegative",
        "businesses",
        type_="check",
    )
    op.drop_column("businesses", "service_distance_fee_per_km")
    op.drop_column("businesses", "service_distance_included_km")
    op.drop_column("businesses", "service_radius_km")

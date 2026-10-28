"""Use Google route calculation for validated operational addresses.

Revision ID: 20261028_0042
Revises: 20261026_0041
Create Date: 2026-10-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20261028_0042"
down_revision = "20261026_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "businesses",
        "travel_calculation_method",
        server_default="route",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    op.execute(
        """
        UPDATE businesses
        SET travel_calculation_method = 'route',
            travel_route_provider = COALESCE(
                travel_route_provider,
                'google_routes'
            )
        WHERE travel_calculation_method = 'configured_estimate'
          AND (
            service_origin_validated_at IS NOT NULL
            OR (
                NULLIF(BTRIM(service_origin_street), '') IS NOT NULL
                AND NULLIF(BTRIM(service_origin_number), '') IS NOT NULL
                AND NULLIF(BTRIM(service_origin_city), '') IS NOT NULL
                AND NULLIF(BTRIM(service_origin_state), '') IS NOT NULL
            )
          )
          AND travel_fallback_allowed IS FALSE
          AND COALESCE(jsonb_array_length(travel_region_rules), 0) = 0
        """
    )


def downgrade() -> None:
    # The data backfill is intentionally not guessed backwards: after migration
    # a route-configured business may have been legitimately edited. Restoring
    # the old default is reversible without overwriting current tenant intent.
    op.alter_column(
        "businesses",
        "travel_calculation_method",
        server_default="configured_estimate",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )

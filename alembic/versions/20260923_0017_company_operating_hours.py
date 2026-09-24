"""Add company operating hours and activate onboarding catalog presets.

Revision ID: 20260923_0017
Revises: 20260923_0016
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260923_0017"
down_revision = "20260923_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "operating_weekdays",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("businesses", sa.Column("weekday_start_time", sa.Time(), nullable=True))
    op.add_column("businesses", sa.Column("weekday_end_time", sa.Time(), nullable=True))
    op.add_column(
        "businesses",
        sa.Column(
            "weekend_holiday_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "businesses",
        sa.Column("weekend_holiday_start_time", sa.Time(), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("weekend_holiday_end_time", sa.Time(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_businesses_weekday_hours_valid"),
        "businesses",
        "weekday_start_time IS NULL OR weekday_end_time IS NULL "
        "OR weekday_end_time > weekday_start_time",
    )
    op.create_check_constraint(
        op.f("ck_businesses_weekend_holiday_hours_valid"),
        "businesses",
        "NOT weekend_holiday_enabled OR "
        "(weekend_holiday_start_time IS NOT NULL "
        "AND weekend_holiday_end_time IS NOT NULL "
        "AND weekend_holiday_end_time > weekend_holiday_start_time)",
    )

    # Presets are suggestions that should be visible by default during onboarding.
    # Businesses that already reviewed their catalog keep their current choices.
    op.execute(
        """
        UPDATE business_catalog_items AS item
        SET active = true, updated_at = now()
        FROM businesses AS business
        WHERE item.business_id = business.id
          AND item.preset_key IS NOT NULL
          AND business.materials_catalog_reviewed = false
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_businesses_weekend_holiday_hours_valid"),
        "businesses",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_businesses_weekday_hours_valid"),
        "businesses",
        type_="check",
    )
    op.drop_column("businesses", "weekend_holiday_end_time")
    op.drop_column("businesses", "weekend_holiday_start_time")
    op.drop_column("businesses", "weekend_holiday_enabled")
    op.drop_column("businesses", "weekday_end_time")
    op.drop_column("businesses", "weekday_start_time")
    op.drop_column("businesses", "operating_weekdays")

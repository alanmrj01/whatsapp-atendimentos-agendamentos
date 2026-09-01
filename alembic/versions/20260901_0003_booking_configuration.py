"""Add configurable booking estimates and operational snapshots.

Revision ID: 20260901_0003
Revises: 20260901_0002
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_0003"
down_revision: str | None = "20260901_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "service_origin_address",
            sa.String(length=500),
            server_default="Zona Leste de São José dos Campos - SP",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "service_origin_latitude",
            sa.Numeric(precision=9, scale=6),
            nullable=True,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "service_origin_longitude",
            sa.Numeric(precision=9, scale=6),
            nullable=True,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "service_origin_is_precise",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_calculation_method",
            sa.String(length=32),
            server_default="configured_estimate",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "default_travel_minutes",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_fallback_allowed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_route_provider",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_before_buffer_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_after_buffer_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "travel_region_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_businesses_default_travel_minutes_nonnegative"),
        "businesses",
        "default_travel_minutes IS NULL OR default_travel_minutes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_businesses_travel_calculation_method_allowed"),
        "businesses",
        "travel_calculation_method IN ('route', 'configured_estimate')",
    )
    op.create_check_constraint(
        op.f("ck_businesses_travel_fallback_requires_minutes"),
        "businesses",
        "NOT travel_fallback_allowed OR default_travel_minutes IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_businesses_service_origin_coordinates_together"),
        "businesses",
        "(service_origin_latitude IS NULL) = "
        "(service_origin_longitude IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_businesses_service_origin_latitude_range"),
        "businesses",
        "service_origin_latitude IS NULL OR "
        "service_origin_latitude BETWEEN -90 AND 90",
    )
    op.create_check_constraint(
        op.f("ck_businesses_service_origin_longitude_range"),
        "businesses",
        "service_origin_longitude IS NULL OR "
        "service_origin_longitude BETWEEN -180 AND 180",
    )
    op.create_check_constraint(
        op.f("ck_businesses_travel_before_buffer_minutes_nonnegative"),
        "businesses",
        "travel_before_buffer_minutes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_businesses_travel_after_buffer_minutes_nonnegative"),
        "businesses",
        "travel_after_buffer_minutes >= 0",
    )

    for column in (
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_price", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "pricing_type",
            sa.String(length=32),
            server_default="estimated",
            nullable=False,
        ),
        sa.Column(
            "automatic_booking",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "included_quantity",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "additional_unit_duration_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("additional_unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "requires_address",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "requires_quantity",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "considers_difficult_access",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "difficult_access_duration_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("difficult_access_price", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "unknown_access_policy",
            sa.String(length=32),
            server_default="conservative",
            nullable=False,
        ),
        sa.Column(
            "duration_margin_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "asks_site_time_limit",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    ):
        op.add_column("services", column)

    for name, condition in (
        (
            "ck_services_pricing_type_allowed",
            "pricing_type IN ('fixed', 'estimated', 'human_quote')",
        ),
        ("ck_services_included_quantity_positive", "included_quantity > 0"),
        (
            "ck_services_additional_unit_duration_minutes_nonnegative",
            "additional_unit_duration_minutes >= 0",
        ),
        (
            "ck_services_additional_unit_price_nonnegative",
            "additional_unit_price IS NULL OR additional_unit_price >= 0",
        ),
        (
            "ck_services_difficult_access_duration_minutes_nonnegative",
            "difficult_access_duration_minutes >= 0",
        ),
        (
            "ck_services_difficult_access_price_nonnegative",
            "difficult_access_price IS NULL OR difficult_access_price >= 0",
        ),
        (
            "ck_services_duration_margin_minutes_nonnegative",
            "duration_margin_minutes >= 0",
        ),
        (
            "ck_services_unknown_access_policy_allowed",
            "unknown_access_policy IN ('standard', 'conservative', 'human_quote')",
        ),
    ):
        op.create_check_constraint(op.f(name), "services", condition)

    op.add_column(
        "appointments",
        sa.Column("service_address", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "access_condition",
            sa.String(length=16),
            server_default="normal",
            nullable=False,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("estimated_duration_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "travel_before_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "travel_after_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("estimated_price", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("pricing_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column(
            "estimate_details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("site_allowed_end", sa.Time(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE appointments AS appointment
        SET estimated_duration_minutes = service.duration_minutes,
            pricing_type = service.pricing_type
        FROM services AS service
        WHERE service.id = appointment.service_id
          AND service.business_id = appointment.business_id
        """
    )
    op.alter_column(
        "appointments", "estimated_duration_minutes", nullable=False
    )
    op.alter_column("appointments", "pricing_type", nullable=False)

    for name, condition in (
        ("ck_appointments_quantity_positive", "quantity > 0"),
        (
            "ck_appointments_estimated_duration_minutes_positive",
            "estimated_duration_minutes > 0",
        ),
        (
            "ck_appointments_travel_before_minutes_nonnegative",
            "travel_before_minutes >= 0",
        ),
        (
            "ck_appointments_travel_after_minutes_nonnegative",
            "travel_after_minutes >= 0",
        ),
        (
            "ck_appointments_pricing_type_allowed",
            "pricing_type IN ('fixed', 'estimated', 'human_quote')",
        ),
        (
            "ck_appointments_access_condition_allowed",
            "access_condition IN ('normal', 'difficult', 'unknown')",
        ),
    ):
        op.create_check_constraint(op.f(name), "appointments", condition)

    op.create_index(
        "uq_appointments_idempotency_key_present",
        "appointments",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_constraint(
        "excl_appointments_employee_confirmed_overlap",
        "appointments",
    )
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT excl_appointments_employee_confirmed_overlap
        EXCLUDE USING gist (
            employee_id WITH =,
            tstzrange(
                starts_at - make_interval(mins => travel_before_minutes),
                ends_at + make_interval(mins => travel_after_minutes),
                '[)'
            ) WITH &&
        ) WHERE (status = 'confirmed')
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "excl_appointments_employee_confirmed_overlap",
        "appointments",
    )
    op.execute(
        """
        ALTER TABLE appointments
        ADD CONSTRAINT excl_appointments_employee_confirmed_overlap
        EXCLUDE USING gist (
            employee_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        ) WHERE (status = 'confirmed')
        """
    )
    op.drop_index(
        "uq_appointments_idempotency_key_present",
        table_name="appointments",
    )
    for name in (
        "ck_appointments_access_condition_allowed",
        "ck_appointments_pricing_type_allowed",
        "ck_appointments_travel_after_minutes_nonnegative",
        "ck_appointments_travel_before_minutes_nonnegative",
        "ck_appointments_estimated_duration_minutes_positive",
        "ck_appointments_quantity_positive",
    ):
        op.drop_constraint(op.f(name), "appointments", type_="check")
    for column_name in (
        "idempotency_key",
        "site_allowed_end",
        "estimate_details",
        "pricing_type",
        "estimated_price",
        "travel_after_minutes",
        "travel_before_minutes",
        "estimated_duration_minutes",
        "access_condition",
        "quantity",
        "service_address",
    ):
        op.drop_column("appointments", column_name)

    for name in (
        "ck_services_unknown_access_policy_allowed",
        "ck_services_duration_margin_minutes_nonnegative",
        "ck_services_difficult_access_price_nonnegative",
        "ck_services_difficult_access_duration_minutes_nonnegative",
        "ck_services_additional_unit_price_nonnegative",
        "ck_services_additional_unit_duration_minutes_nonnegative",
        "ck_services_included_quantity_positive",
        "ck_services_pricing_type_allowed",
    ):
        op.drop_constraint(op.f(name), "services", type_="check")
    for column_name in (
        "asks_site_time_limit",
        "duration_margin_minutes",
        "unknown_access_policy",
        "difficult_access_price",
        "difficult_access_duration_minutes",
        "considers_difficult_access",
        "requires_quantity",
        "requires_address",
        "additional_unit_price",
        "additional_unit_duration_minutes",
        "included_quantity",
        "automatic_booking",
        "pricing_type",
        "base_price",
        "description",
    ):
        op.drop_column("services", column_name)

    for name in (
        "ck_businesses_travel_after_buffer_minutes_nonnegative",
        "ck_businesses_travel_before_buffer_minutes_nonnegative",
        "ck_businesses_service_origin_longitude_range",
        "ck_businesses_service_origin_latitude_range",
        "ck_businesses_service_origin_coordinates_together",
        "ck_businesses_travel_fallback_requires_minutes",
        "ck_businesses_travel_calculation_method_allowed",
        "ck_businesses_default_travel_minutes_nonnegative",
    ):
        op.drop_constraint(op.f(name), "businesses", type_="check")
    for column_name in (
        "travel_region_rules",
        "travel_after_buffer_minutes",
        "travel_before_buffer_minutes",
        "travel_route_provider",
        "travel_fallback_allowed",
        "default_travel_minutes",
        "travel_calculation_method",
        "service_origin_is_precise",
        "service_origin_longitude",
        "service_origin_latitude",
        "service_origin_address",
    ):
        op.drop_column("businesses", column_name)

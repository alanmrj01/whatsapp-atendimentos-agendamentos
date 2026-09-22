"""Add guided onboarding, service semantics and materials catalog.

Revision ID: 20260922_0012
Revises: 20260921_0011
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260922_0012"
down_revision = "20260921_0011"
branch_labels = None
depends_on = None


_PRESETS = (
    (
        "extra_copper_pipe_meter",
        "Metro adicional de tubulação",
        "Tubulação adicional acima da metragem incluída no serviço.",
        "meter",
    ),
    (
        "electrical_cable_meter",
        "Metro adicional de cabo elétrico",
        "Cabo adicional utilizado na instalação.",
        "meter",
    ),
    (
        "drain_hose_meter",
        "Metro adicional de dreno",
        "Mangueira ou tubulação adicional para drenagem.",
        "meter",
    ),
    (
        "outdoor_unit_bracket",
        "Suporte para condensadora",
        "Suporte adicional para instalação da unidade externa.",
        "unit",
    ),
    (
        "thermal_insulation_meter",
        "Metro adicional de isolamento",
        "Isolamento térmico adicional utilizado na instalação.",
        "meter",
    ),
)


def upgrade() -> None:
    op.add_column("businesses", sa.Column("responsible_name", sa.String(length=255), nullable=True))
    op.add_column(
        "businesses",
        sa.Column(
            "service_origin_configured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "businesses",
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "onboarding_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column("materials_catalog_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("agenda_settings_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("default_service_gap_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("default_preparation_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("default_completion_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("minimum_booking_notice_minutes", sa.Integer(), nullable=True),
    )
    for name in (
        "default_service_gap_minutes",
        "default_preparation_minutes",
        "default_completion_minutes",
    ):
        op.create_check_constraint(
            op.f(f"ck_businesses_{name}_range"),
            "businesses",
            f"{name} IS NULL OR ({name} >= 0 AND {name} <= 50)",
        )
    op.create_check_constraint(
        op.f("ck_businesses_minimum_booking_notice_minutes_range"),
        "businesses",
        "minimum_booking_notice_minutes IS NULL OR "
        "(minimum_booking_notice_minutes >= 0 AND minimum_booking_notice_minutes <= 10080)",
    )

    op.add_column(
        "services",
        sa.Column(
            "interpretation_examples",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("services", sa.Column("service_gap_minutes", sa.Integer(), nullable=True))
    op.add_column("services", sa.Column("preparation_minutes", sa.Integer(), nullable=True))
    op.add_column("services", sa.Column("completion_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "services",
        sa.Column("minimum_booking_notice_minutes", sa.Integer(), nullable=True),
    )
    for name in ("service_gap_minutes", "preparation_minutes", "completion_minutes"):
        op.create_check_constraint(
            op.f(f"ck_services_{name}_range"),
            "services",
            f"{name} IS NULL OR ({name} >= 0 AND {name} <= 50)",
        )
    op.create_check_constraint(
        op.f("ck_services_minimum_booking_notice_minutes_range"),
        "services",
        "minimum_booking_notice_minutes IS NULL OR "
        "(minimum_booking_notice_minutes >= 0 AND minimum_booking_notice_minutes <= 10080)",
    )

    op.create_table(
        "business_catalog_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=False, server_default="unit"),
        sa.Column("preset_key", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("price IS NULL OR price >= 0", name=op.f("ck_business_catalog_items_price_nonnegative")),
        sa.CheckConstraint("unit IN ('unit', 'meter')", name=op.f("ck_business_catalog_items_unit_allowed")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_business_catalog_items_business_id"),
        "business_catalog_items",
        ["business_id"],
        unique=False,
    )
    op.create_index(
        "uq_business_catalog_items_business_preset_present",
        "business_catalog_items",
        ["business_id", "preset_key"],
        unique=True,
        postgresql_where=sa.text("preset_key IS NOT NULL"),
    )

    op.execute(
        """
        UPDATE services
        SET interpretation_examples = jsonb_build_array(
            'quero ' || name,
            'preciso de ' || name,
            'vocês fazem ' || name,
            'quanto custa ' || name,
            'quero agendar ' || name,
            'gostaria de fazer ' || name,
            'preciso marcar ' || name,
            'tem horário para ' || name
        )
        WHERE interpretation_examples = '[]'::jsonb
        """
    )

    values = ",\n".join(
        "('{}', '{}', '{}', '{}')".format(
            key,
            name.replace("'", "''"),
            description.replace("'", "''"),
            unit,
        )
        for key, name, description, unit in _PRESETS
    )
    op.execute(
        f"""
        WITH presets(preset_key, name, description, unit) AS (
            VALUES {values}
        )
        INSERT INTO business_catalog_items (
            id, business_id, name, description, price, unit, preset_key, active,
            created_at, updated_at
        )
        SELECT
            (
                substr(md5(b.id::text || '|catalog|' || p.preset_key),1,8)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),9,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),13,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),17,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),21,12)
            )::uuid,
            b.id, p.name, p.description, NULL, p.unit, p.preset_key, false,
            now(), now()
        FROM businesses AS b
        CROSS JOIN presets AS p
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(
        "uq_business_catalog_items_business_preset_present",
        table_name="business_catalog_items",
    )
    op.drop_index(op.f("ix_business_catalog_items_business_id"), table_name="business_catalog_items")
    op.drop_table("business_catalog_items")

    for name in (
        "minimum_booking_notice_minutes",
        "completion_minutes",
        "preparation_minutes",
        "service_gap_minutes",
    ):
        if name == "minimum_booking_notice_minutes":
            op.drop_constraint(
                op.f("ck_services_minimum_booking_notice_minutes_range"),
                "services",
                type_="check",
            )
        else:
            op.drop_constraint(
                op.f(f"ck_services_{name}_range"),
                "services",
                type_="check",
            )
        op.drop_column("services", name)
    op.drop_column("services", "interpretation_examples")

    op.drop_constraint(
        op.f("ck_businesses_minimum_booking_notice_minutes_range"),
        "businesses",
        type_="check",
    )
    for name in (
        "default_completion_minutes",
        "default_preparation_minutes",
        "default_service_gap_minutes",
    ):
        op.drop_constraint(
            op.f(f"ck_businesses_{name}_range"),
            "businesses",
            type_="check",
        )
    for name in (
        "minimum_booking_notice_minutes",
        "default_completion_minutes",
        "default_preparation_minutes",
        "default_service_gap_minutes",
        "agenda_settings_reviewed_at",
        "materials_catalog_reviewed_at",
        "onboarding_version",
        "onboarding_completed_at",
        "service_origin_configured",
        "responsible_name",
    ):
        op.drop_column("businesses", name)

"""Add onboarding, catalogs and assistant service semantics.

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


def upgrade() -> None:
    op.add_column("businesses", sa.Column("responsible_name", sa.String(length=255), nullable=True))
    op.add_column("businesses", sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("businesses", sa.Column("onboarding_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("businesses", sa.Column("materials_catalog_reviewed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("businesses", sa.Column("agenda_preferences_reviewed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("businesses", sa.Column("interval_between_services_minutes", sa.Integer(), nullable=True))
    op.add_column("businesses", sa.Column("preparation_minutes", sa.Integer(), nullable=True))
    op.add_column("businesses", sa.Column("finishing_minutes", sa.Integer(), nullable=True))
    op.add_column("businesses", sa.Column("minimum_booking_notice_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_businesses_interval_between_services_nonnegative"),
        "businesses",
        "interval_between_services_minutes IS NULL OR interval_between_services_minutes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_businesses_preparation_minutes_nonnegative"),
        "businesses",
        "preparation_minutes IS NULL OR preparation_minutes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_businesses_finishing_minutes_nonnegative"),
        "businesses",
        "finishing_minutes IS NULL OR finishing_minutes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_businesses_minimum_booking_notice_nonnegative"),
        "businesses",
        "minimum_booking_notice_minutes IS NULL OR minimum_booking_notice_minutes >= 0",
    )
    op.alter_column("businesses", "service_origin_address", existing_type=sa.String(length=500), nullable=True, server_default=None)

    op.add_column(
        "services",
        sa.Column(
            "intent_examples",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_table(
        "business_catalog_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("unit_label", sa.String(length=64), nullable=True),
        sa.Column("preset_key", sa.String(length=128), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('material', 'equipment')", name=op.f("ck_business_catalog_items_kind_allowed")),
        sa.CheckConstraint("price IS NULL OR price >= 0", name=op.f("ck_business_catalog_items_price_nonnegative")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_business_catalog_items_business_id_businesses")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_catalog_items")),
        sa.UniqueConstraint("business_id", "preset_key", name="uq_business_catalog_items_business_preset"),
    )
    op.create_index(op.f("ix_business_catalog_items_business_id"), "business_catalog_items", ["business_id"], unique=False)
    op.create_index(op.f("ix_business_catalog_items_active"), "business_catalog_items", ["active"], unique=False)

    op.execute(
        """
        INSERT INTO business_catalog_items (
            id, business_id, kind, name, description, price, unit_label,
            preset_key, active, created_at, updated_at
        )
        SELECT
            (
                substr(md5(b.id::text || '|catalog|' || p.preset_key),1,8)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),9,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),13,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),17,4)
                ||'-'||substr(md5(b.id::text || '|catalog|' || p.preset_key),21,12)
            )::uuid,
            b.id, p.kind, p.name, p.description, NULL, p.unit_label,
            p.preset_key, false, now(), now()
        FROM businesses AS b
        CROSS JOIN (
            VALUES
              ('extra-tubing-meter','material','Metro adicional de tubulação','Cobrança por metro acima da metragem incluída no serviço.','metro'),
              ('extra-drain-meter','material','Metro adicional de dreno','Material adicional de drenagem quando necessário.','metro'),
              ('extra-electrical-cable-meter','material','Metro adicional de cabo elétrico','Cabo elétrico adicional utilizado na instalação.','metro'),
              ('condenser-bracket','equipment','Suporte para condensadora','Suporte utilizado na instalação da unidade externa.','unidade'),
              ('wall-bracket-fixings','material','Kit de fixação','Parafusos, buchas e itens de fixação adicionais.','kit')
        ) AS p(preset_key, kind, name, description, unit_label)
        ON CONFLICT (business_id, preset_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_business_catalog_items_active"), table_name="business_catalog_items")
    op.drop_index(op.f("ix_business_catalog_items_business_id"), table_name="business_catalog_items")
    op.drop_table("business_catalog_items")
    op.drop_column("services", "intent_examples")
    op.alter_column("businesses", "service_origin_address", existing_type=sa.String(length=500), nullable=False)
    op.drop_constraint(op.f("ck_businesses_minimum_booking_notice_nonnegative"), "businesses", type_="check")
    op.drop_constraint(op.f("ck_businesses_finishing_minutes_nonnegative"), "businesses", type_="check")
    op.drop_constraint(op.f("ck_businesses_preparation_minutes_nonnegative"), "businesses", type_="check")
    op.drop_constraint(op.f("ck_businesses_interval_between_services_nonnegative"), "businesses", type_="check")
    op.drop_column("businesses", "minimum_booking_notice_minutes")
    op.drop_column("businesses", "finishing_minutes")
    op.drop_column("businesses", "preparation_minutes")
    op.drop_column("businesses", "interval_between_services_minutes")
    op.drop_column("businesses", "agenda_preferences_reviewed")
    op.drop_column("businesses", "materials_catalog_reviewed")
    op.drop_column("businesses", "onboarding_version")
    op.drop_column("businesses", "onboarding_completed_at")
    op.drop_column("businesses", "responsible_name")

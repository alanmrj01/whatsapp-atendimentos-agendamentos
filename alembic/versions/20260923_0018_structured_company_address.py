"""Add structured company origin address fields.

Revision ID: 20260923_0018
Revises: 20260923_0017
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260923_0018"
down_revision = "20260923_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("service_origin_postal_code", sa.String(length=8), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_street", sa.String(length=255), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_neighborhood", sa.String(length=255), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_number", sa.String(length=32), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_city", sa.String(length=255), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_state", sa.String(length=2), nullable=True))
    op.add_column("businesses", sa.Column("service_origin_validated_at", sa.DateTime(timezone=True), nullable=True))

    op.create_check_constraint(
        op.f("ck_businesses_service_origin_postal_code_format"),
        "businesses",
        "service_origin_postal_code IS NULL OR service_origin_postal_code ~ '^[0-9]{8}$'",
    )
    op.create_check_constraint(
        op.f("ck_businesses_service_origin_state_format"),
        "businesses",
        "service_origin_state IS NULL OR service_origin_state ~ '^[A-Z]{2}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_businesses_service_origin_state_format"),
        "businesses",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_businesses_service_origin_postal_code_format"),
        "businesses",
        type_="check",
    )
    op.drop_column("businesses", "service_origin_validated_at")
    op.drop_column("businesses", "service_origin_state")
    op.drop_column("businesses", "service_origin_city")
    op.drop_column("businesses", "service_origin_number")
    op.drop_column("businesses", "service_origin_neighborhood")
    op.drop_column("businesses", "service_origin_street")
    op.drop_column("businesses", "service_origin_postal_code")

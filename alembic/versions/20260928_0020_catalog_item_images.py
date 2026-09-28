"""Store tenant-uploaded catalog equipment images.

Revision ID: 20260928_0020
Revises: 20260926_0019
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20260928_0020"
down_revision = "20260926_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_catalog_items",
        sa.Column("image_data", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "business_catalog_items",
        sa.Column("image_mime_type", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("business_catalog_items", "image_mime_type")
    op.drop_column("business_catalog_items", "image_data")

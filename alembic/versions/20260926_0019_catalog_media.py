"""Add rich equipment catalog and WhatsApp media metadata.

Revision ID: 20260926_0019
Revises: 20260923_0018
Create Date: 2026-09-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260926_0019"
down_revision = "20260923_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_catalog_items",
        sa.Column("image_url", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "business_catalog_items",
        sa.Column("source_url", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "business_catalog_items",
        sa.Column(
            "specifications",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.add_column(
        "messages",
        sa.Column("media_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("media_mime_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("media_filename", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("media_sha256", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "media_sha256")
    op.drop_column("messages", "media_filename")
    op.drop_column("messages", "media_mime_type")
    op.drop_column("messages", "media_id")

    op.drop_column("business_catalog_items", "specifications")
    op.drop_column("business_catalog_items", "source_url")
    op.drop_column("business_catalog_items", "image_url")

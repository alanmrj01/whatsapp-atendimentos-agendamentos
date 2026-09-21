"""Add conversation inbox controls.

Revision ID: 20260921_0012
Revises: 20260921_0011
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0012"
down_revision = "20260921_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "manual_unread",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_conversations_pinned_at"),
        "conversations",
        ["pinned_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversations_archived_at"),
        "conversations",
        ["archived_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_conversations_archived_at"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_pinned_at"), table_name="conversations")
    op.drop_column("conversations", "archived_at")
    op.drop_column("conversations", "manual_unread")
    op.drop_column("conversations", "last_read_at")
    op.drop_column("conversations", "pinned_at")

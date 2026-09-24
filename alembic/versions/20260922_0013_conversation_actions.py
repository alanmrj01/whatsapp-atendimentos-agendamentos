"""Add conversation workspace actions.

Revision ID: 20260922_0013
Revises: 20260922_0012
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa

revision = "20260922_0013"
down_revision = "20260922_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("manual_unread", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        op.f("ix_conversations_pinned_at"),
        "conversations",
        ["pinned_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversations_deleted_at"),
        "conversations",
        ["deleted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_conversations_deleted_at"), table_name="conversations")
    op.drop_index(op.f("ix_conversations_pinned_at"), table_name="conversations")
    op.drop_column("conversations", "deleted_at")
    op.drop_column("conversations", "manual_unread")
    op.drop_column("conversations", "last_read_at")
    op.drop_column("conversations", "pinned_at")

"""Add outbound message send timestamps for plan quotas.

Revision ID: 20261026_0041
Revises: 20261022_0040
Create Date: 2026-10-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20261026_0041"
down_revision = "20261022_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE messages
           SET sent_at = created_at
         WHERE direction = 'outbound'
           AND status IN ('sent', 'delivered', 'read')
           AND sent_at IS NULL
        """
    )
    op.create_index(
        "ix_messages_business_sent_at",
        "messages",
        ["business_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_business_sent_at", table_name="messages")
    op.drop_column("messages", "sent_at")

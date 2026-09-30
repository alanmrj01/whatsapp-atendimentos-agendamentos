"""Polish assistant default messages.

Revision ID: 20260930_0023
Revises: 20260930_0022
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0023"
down_revision = "20260930_0022"
branch_labels = None
depends_on = None

OLD_FALLBACK = "Não entendi. Conte em poucas palavras o serviço que você precisa."
NEW_FALLBACK = "Desculpe, não entendi. Conte em poucas palavras o serviço que você precisa."
OLD_HANDOFF = "Seu atendimento foi encaminhado para uma pessoa da equipe."
NEW_HANDOFF = (
    "Seu atendimento foi encaminhado para uma pessoa da equipe. "
    "Por favor, aguarde alguns instantes."
)


def upgrade() -> None:
    businesses = sa.table(
        "businesses",
        sa.column("assistant_fallback_message", sa.String(length=1000)),
        sa.column("assistant_handoff_message", sa.String(length=1000)),
    )
    op.execute(
        businesses.update()
        .where(businesses.c.assistant_fallback_message == OLD_FALLBACK)
        .values(assistant_fallback_message=NEW_FALLBACK)
    )
    op.execute(
        businesses.update()
        .where(businesses.c.assistant_handoff_message == OLD_HANDOFF)
        .values(assistant_handoff_message=NEW_HANDOFF)
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=NEW_FALLBACK,
        existing_type=sa.String(length=1000),
        existing_nullable=False,
    )
    op.alter_column(
        "businesses",
        "assistant_handoff_message",
        server_default=NEW_HANDOFF,
        existing_type=sa.String(length=1000),
        existing_nullable=False,
    )


def downgrade() -> None:
    businesses = sa.table(
        "businesses",
        sa.column("assistant_fallback_message", sa.String(length=1000)),
        sa.column("assistant_handoff_message", sa.String(length=1000)),
    )
    op.execute(
        businesses.update()
        .where(businesses.c.assistant_fallback_message == NEW_FALLBACK)
        .values(assistant_fallback_message=OLD_FALLBACK)
    )
    op.execute(
        businesses.update()
        .where(businesses.c.assistant_handoff_message == NEW_HANDOFF)
        .values(assistant_handoff_message=OLD_HANDOFF)
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=OLD_FALLBACK,
        existing_type=sa.String(length=1000),
        existing_nullable=False,
    )
    op.alter_column(
        "businesses",
        "assistant_handoff_message",
        server_default=OLD_HANDOFF,
        existing_type=sa.String(length=1000),
        existing_nullable=False,
    )

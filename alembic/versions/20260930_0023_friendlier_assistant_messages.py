"""Refresh default assistant messages.

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
    op.execute(
        sa.text(
            "UPDATE businesses SET assistant_fallback_message = :new "
            "WHERE assistant_fallback_message = :old"
        ).bindparams(new=NEW_FALLBACK, old=OLD_FALLBACK)
    )
    op.execute(
        sa.text(
            "UPDATE businesses SET assistant_handoff_message = :new "
            "WHERE assistant_handoff_message = :old"
        ).bindparams(new=NEW_HANDOFF, old=OLD_HANDOFF)
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=NEW_FALLBACK,
    )
    op.alter_column(
        "businesses",
        "assistant_handoff_message",
        server_default=NEW_HANDOFF,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE businesses SET assistant_fallback_message = :old "
            "WHERE assistant_fallback_message = :new"
        ).bindparams(old=OLD_FALLBACK, new=NEW_FALLBACK)
    )
    op.execute(
        sa.text(
            "UPDATE businesses SET assistant_handoff_message = :old "
            "WHERE assistant_handoff_message = :new"
        ).bindparams(old=OLD_HANDOFF, new=NEW_HANDOFF)
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=OLD_FALLBACK,
    )
    op.alter_column(
        "businesses",
        "assistant_handoff_message",
        server_default=OLD_HANDOFF,
    )

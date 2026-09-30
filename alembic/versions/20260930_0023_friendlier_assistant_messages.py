"""Refresh default assistant messages.

Revision ID: 20260930_0023
Revises: 20260930_0022
Create Date: 2026-09-30
"""

from alembic import op

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
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE businesses SET assistant_fallback_message = %s "
        "WHERE assistant_fallback_message = %s",
        (NEW_FALLBACK, OLD_FALLBACK),
    )
    bind.exec_driver_sql(
        "UPDATE businesses SET assistant_handoff_message = %s "
        "WHERE assistant_handoff_message = %s",
        (NEW_HANDOFF, OLD_HANDOFF),
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
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE businesses SET assistant_fallback_message = %s "
        "WHERE assistant_fallback_message = %s",
        (OLD_FALLBACK, NEW_FALLBACK),
    )
    bind.exec_driver_sql(
        "UPDATE businesses SET assistant_handoff_message = %s "
        "WHERE assistant_handoff_message = %s",
        (OLD_HANDOFF, NEW_HANDOFF),
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

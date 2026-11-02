"""Use a single concise unknown-message fallback.

Revision ID: 20261102_0042
Revises: 20261026_0041
Create Date: 2026-11-02
"""

from alembic import op
import sqlalchemy as sa

revision = "20261102_0042"
down_revision = "20261026_0041"
branch_labels = None
depends_on = None

OLD_FALLBACKS = (
    "Não entendi. Conte em poucas palavras o serviço que você precisa.",
    "Desculpe, não entendi. Conte em poucas palavras o serviço que você precisa.",
)
NEW_FALLBACK = "Desculpa, não entendi. como posso te ajudar?"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE businesses "
            "SET assistant_fallback_message = :new "
            "WHERE assistant_fallback_message IN (:old_one, :old_two)"
        ).bindparams(
            new=NEW_FALLBACK,
            old_one=OLD_FALLBACKS[0],
            old_two=OLD_FALLBACKS[1],
        )
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=NEW_FALLBACK,
    )


def downgrade() -> None:
    previous = OLD_FALLBACKS[1]
    op.execute(
        sa.text(
            "UPDATE businesses "
            "SET assistant_fallback_message = :previous "
            "WHERE assistant_fallback_message = :new"
        ).bindparams(previous=previous, new=NEW_FALLBACK)
    )
    op.alter_column(
        "businesses",
        "assistant_fallback_message",
        server_default=previous,
    )

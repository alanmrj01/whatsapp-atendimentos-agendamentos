"""Add independent quiz-result lead contacts with contact permission.

Revision ID: 20261120_0047
Revises: 20261118_0046
Create Date: 2026-11-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261120_0047"
down_revision = "20261118_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketing_quiz_leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_name", sa.String(length=120), nullable=False),
        sa.Column("whatsapp_e164", sa.String(length=16), nullable=False),
        sa.Column("quiz_profile", sa.String(length=32), nullable=False),
        sa.Column("ad_id", sa.String(length=120), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_marketing_quiz_leads"),
        sa.UniqueConstraint("whatsapp_e164", name="uq_marketing_quiz_leads_whatsapp_e164"),
        sa.CheckConstraint(
            "whatsapp_e164 ~ '^\\+55[1-9][0-9]{9,10}$'",
            name="ck_marketing_quiz_leads_whatsapp_e164_brazilian",
        ),
    )
    op.create_index("ix_marketing_quiz_leads_created", "marketing_quiz_leads", ["created_at"])
    op.execute('ALTER TABLE "marketing_quiz_leads" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.execute('ALTER TABLE "marketing_quiz_leads" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_marketing_quiz_leads_created", table_name="marketing_quiz_leads")
    op.drop_table("marketing_quiz_leads")

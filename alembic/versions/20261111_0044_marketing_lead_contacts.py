"""Add isolated marketing lead contacts.

Revision ID: 20261111_0044
Revises: 20261107_0043
Create Date: 2026-11-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261111_0044"
down_revision = "20261107_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketing_lead_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_name", sa.String(length=120), nullable=False),
        sa.Column("whatsapp_e164", sa.String(length=16), nullable=False),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default=sa.text("'signup'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "whatsapp_e164 ~ '^\\+55[1-9][0-9]{9,10}$'",
            name="ck_marketing_lead_contacts_whatsapp_e164_brazilian",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name="fk_marketing_lead_contacts_business_id_businesses",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_marketing_lead_contacts_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_marketing_lead_contacts"),
        sa.UniqueConstraint(
            "business_id",
            name="uq_marketing_lead_contacts_business_id",
        ),
    )
    op.create_index(
        "ix_marketing_lead_contacts_created",
        "marketing_lead_contacts",
        ["created_at"],
    )
    op.execute('ALTER TABLE "marketing_lead_contacts" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.execute('ALTER TABLE "marketing_lead_contacts" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "ix_marketing_lead_contacts_created",
        table_name="marketing_lead_contacts",
    )
    op.drop_table("marketing_lead_contacts")

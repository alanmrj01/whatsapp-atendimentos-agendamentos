"""Preserve operational access history for former paid/admin-granted tenants.

Revision ID: 20260915_0009
Revises: 20260908_0008
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260915_0009"
down_revision = "20260908_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_access",
        sa.Column(
            "has_had_operational_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Paid tenants certainly have operational history. For free rows that may
    # have been revoked before this flag existed, infer history conservatively
    # from persisted operational records. Nothing is deleted or rewritten.
    op.execute(
        """
        UPDATE business_access AS ba
        SET has_had_operational_access = true
        WHERE ba.access_mode = 'paid'
           OR EXISTS (
                SELECT 1 FROM business_whatsapp_connections bwc
                WHERE bwc.business_id = ba.business_id
           )
           OR EXISTS (
                SELECT 1 FROM conversations c
                WHERE c.business_id = ba.business_id
           )
           OR EXISTS (
                SELECT 1 FROM appointments a
                WHERE a.business_id = ba.business_id
           )
           OR EXISTS (
                SELECT 1 FROM customers cu
                WHERE cu.business_id = ba.business_id
           )
        """
    )


def downgrade() -> None:
    op.drop_column("business_access", "has_had_operational_access")

"""Add customer-level automation policy.

Revision ID: 20261107_0043
Revises: 20261102_0042
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa

revision = "20261107_0043"
down_revision = "20261102_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "automation_mode",
            sa.String(length=32),
            nullable=False,
            server_default="automatic",
        ),
    )
    op.create_check_constraint(
        "automation_mode_allowed",
        "customers",
        "automation_mode IN ('automatic', 'human_only', 'ignore')",
    )
    op.create_index(
        "ix_customers_business_automation_mode",
        "customers",
        ["business_id", "automation_mode"],
        unique=False,
    )

    # Existing exclusion rows remain supported as a compatibility layer. Where a
    # customer already exists, copy the active policy into the customer so the
    # customer record becomes the primary runtime source immediately.
    op.execute(
        sa.text(
            """
            UPDATE customers AS c
            SET automation_mode = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM business_automation_exclusions AS e
                    WHERE e.business_id = c.business_id
                      AND e.active IS TRUE
                      AND e.mode = 'ignore'
                      AND (
                        e.whatsapp_id = c.whatsapp_id
                        OR (
                            c.whatsapp_id LIKE '55%'
                            AND e.whatsapp_id = substring(c.whatsapp_id from 3)
                        )
                        OR (
                            e.whatsapp_id LIKE '55%'
                            AND c.whatsapp_id = substring(e.whatsapp_id from 3)
                        )
                      )
                ) THEN 'ignore'
                WHEN EXISTS (
                    SELECT 1
                    FROM business_automation_exclusions AS e
                    WHERE e.business_id = c.business_id
                      AND e.active IS TRUE
                      AND e.mode = 'human_only'
                      AND (
                        e.whatsapp_id = c.whatsapp_id
                        OR (
                            c.whatsapp_id LIKE '55%'
                            AND e.whatsapp_id = substring(c.whatsapp_id from 3)
                        )
                        OR (
                            e.whatsapp_id LIKE '55%'
                            AND c.whatsapp_id = substring(e.whatsapp_id from 3)
                        )
                      )
                ) THEN 'human_only'
                ELSE 'automatic'
            END
            WHERE EXISTS (
                SELECT 1
                FROM business_automation_exclusions AS e
                WHERE e.business_id = c.business_id
                  AND e.active IS TRUE
                  AND (
                    e.whatsapp_id = c.whatsapp_id
                    OR (
                        c.whatsapp_id LIKE '55%'
                        AND e.whatsapp_id = substring(c.whatsapp_id from 3)
                    )
                    OR (
                        e.whatsapp_id LIKE '55%'
                        AND c.whatsapp_id = substring(e.whatsapp_id from 3)
                    )
                  )
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customers_business_automation_mode",
        table_name="customers",
    )
    op.drop_constraint(
        "automation_mode_allowed",
        "customers",
        type_="check",
    )
    op.drop_column("customers", "automation_mode")

"""Add operational defaults, assistant settings and contact identity.

Revision ID: 20260921_0011
Revises: 20260915_0010
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "20260921_0011"
down_revision = "20260915_0010"
branch_labels = None
depends_on = None


DEFAULT_SERVICES = (
    ("split-installation", "Instalação de ar-condicionado split", 180),
    ("cleaning", "Limpeza e higienização", 90),
    ("preventive-maintenance", "Manutenção preventiva", 90),
    ("diagnostics", "Diagnóstico / manutenção corretiva", 120),
    ("gas-recharge", "Recarga de gás e teste de vazamento", 120),
)


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("assistant_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "assistant_greeting_message",
            sa.String(length=1000),
            nullable=False,
            server_default="Olá! Como posso ajudar com seu ar-condicionado?",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "assistant_fallback_message",
            sa.String(length=1000),
            nullable=False,
            server_default="Não entendi. Conte em poucas palavras o serviço que você precisa.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "assistant_handoff_message",
            sa.String(length=1000),
            nullable=False,
            server_default="Seu atendimento foi encaminhado para uma pessoa da equipe.",
        ),
    )
    op.add_column(
        "employees",
        sa.Column("operational_role", sa.String(length=32), nullable=False, server_default="technician"),
    )
    op.create_check_constraint(
        op.f("ck_employees_operational_role_allowed"),
        "employees",
        "operational_role IN ('technician', 'assistant', 'administrator')",
    )
    op.add_column(
        "customers",
        sa.Column("whatsapp_profile_name", sa.String(length=255), nullable=True),
    )

    default_rows = ",\n".join(
        "('{}', '{}', {})".format(key, name.replace("'", "''"), duration)
        for key, name, duration in DEFAULT_SERVICES
    )
    op.execute(
        f"""
        WITH empty_businesses AS MATERIALIZED (
            SELECT b.id
            FROM businesses AS b
            WHERE NOT EXISTS (
                SELECT 1 FROM services AS existing
                WHERE existing.business_id = b.id
            )
        ), defaults(service_key, name, duration_minutes) AS (
            VALUES {default_rows}
        )
        INSERT INTO services (
            id, business_id, name, description, duration_minutes,
            base_price, pricing_type, automatic_booking,
            included_quantity, additional_unit_duration_minutes,
            additional_unit_price, requires_address, requires_quantity,
            considers_difficult_access, difficult_access_duration_minutes,
            difficult_access_price, unknown_access_policy,
            duration_margin_minutes, asks_site_time_limit, active,
            created_at, updated_at
        )
        SELECT
            (
                substr(md5(b.id::text || '|alovia-default|' || defaults.service_key),1,8)
                ||'-'||substr(md5(b.id::text || '|alovia-default|' || defaults.service_key),9,4)
                ||'-'||substr(md5(b.id::text || '|alovia-default|' || defaults.service_key),13,4)
                ||'-'||substr(md5(b.id::text || '|alovia-default|' || defaults.service_key),17,4)
                ||'-'||substr(md5(b.id::text || '|alovia-default|' || defaults.service_key),21,12)
            )::uuid,
            b.id, defaults.name, NULL, defaults.duration_minutes,
            NULL, 'estimated', true,
            1, 0, NULL, true, false,
            false, 0, NULL, 'conservative',
            0, false, true, now(), now()
        FROM empty_businesses AS b
        CROSS JOIN defaults
        """
    )


def downgrade() -> None:
    # Default services are deliberately preserved: a downgrade must not delete
    # operational data that may already have been edited by a tenant.
    op.drop_column("customers", "whatsapp_profile_name")
    op.drop_constraint(
        op.f("ck_employees_operational_role_allowed"),
        "employees",
        type_="check",
    )
    op.drop_column("employees", "operational_role")
    op.drop_column("businesses", "assistant_handoff_message")
    op.drop_column("businesses", "assistant_fallback_message")
    op.drop_column("businesses", "assistant_greeting_message")
    op.drop_column("businesses", "assistant_enabled")

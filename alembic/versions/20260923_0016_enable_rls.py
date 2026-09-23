"""Enable RLS on all application tables exposed through public schema.

Revision ID: 20260923_0016
Revises: 20260922_0015
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0016"
down_revision = "20260922_0015"
branch_labels = None
depends_on = None

_TABLES = (
    "alembic_version",
    "businesses",
    "customers",
    "conversations",
    "services",
    "employees",
    "employee_services",
    "working_hours",
    "schedule_blocks",
    "appointments",
    "messages",
    "processed_webhooks",
    "business_automation_exclusions",
    "business_whatsapp_connections",
    "users",
    "business_user_memberships",
    "auth_sessions",
    "business_access",
    "billing_checkouts",
    "commercial_subscriptions",
    "billing_webhook_events",
    "business_catalog_items",
    "business_notifications",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        """
        ALTER FUNCTION public.booking_add_minutes_immutable(
            timestamp with time zone,
            integer
        ) SET search_path = pg_catalog, public
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER FUNCTION public.booking_add_minutes_immutable(
            timestamp with time zone,
            integer
        ) RESET search_path
        """
    )
    for table in reversed(_TABLES):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')

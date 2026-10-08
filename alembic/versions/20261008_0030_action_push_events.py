"""Allow action-required Web Push event categories.

Revision ID: 20261008_0030
Revises: 20261004_0029
Create Date: 2026-10-08
"""

from alembic import op

revision = "20261008_0030"
down_revision = "20261004_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_web_push_events_event_type_allowed"),
        "web_push_events",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_web_push_events_event_type_allowed"),
        "web_push_events",
        "event_type IN ('inbound_message', 'automatic_booking', "
        "'billing_attention', 'whatsapp_connection_attention')",
    )


def downgrade() -> None:
    # The downgrade is intentionally fail-closed if newer event types remain.
    op.drop_constraint(
        op.f("ck_web_push_events_event_type_allowed"),
        "web_push_events",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_web_push_events_event_type_allowed"),
        "web_push_events",
        "event_type IN ('inbound_message', 'automatic_booking')",
    )

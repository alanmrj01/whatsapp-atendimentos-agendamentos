"""Persist WhatsApp onboarding intent and action-only push types.

Revision ID: 20261007_0030
Revises: 20261004_0029
"""

from alembic import op
import sqlalchemy as sa

revision = "20261007_0030"
down_revision = "20261004_0029"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("businesses", sa.Column("whatsapp_desired_mode", sa.String(16), nullable=True))
    op.add_column("businesses", sa.Column("whatsapp_setup_source", sa.String(32), nullable=True))
    op.add_column("businesses", sa.Column("whatsapp_review_status", sa.String(16), nullable=True))
    op.add_column("businesses", sa.Column("whatsapp_review_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("businesses", sa.Column("whatsapp_review_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("whatsapp_desired_mode_allowed", "businesses", "whatsapp_desired_mode IS NULL OR whatsapp_desired_mode IN ('coexistence', 'api_only')")
    op.create_check_constraint("whatsapp_setup_source_allowed", "businesses", "whatsapp_setup_source IS NULL OR whatsapp_setup_source IN ('business_app', 'migrated_to_business', 'exclusive')")
    op.create_check_constraint("whatsapp_review_status_allowed", "businesses", "whatsapp_review_status IS NULL OR whatsapp_review_status IN ('unknown', 'pending', 'approved', 'rejected')")
    op.drop_constraint("event_type_allowed", "web_push_events", type_="check")
    op.create_check_constraint("event_type_allowed", "web_push_events", "event_type IN ('inbound_message', 'automatic_booking', 'billing_due', 'billing_past_due', 'whatsapp_coexistence_ready', 'whatsapp_connection_attention')")

def downgrade() -> None:
    op.drop_constraint("event_type_allowed", "web_push_events", type_="check")
    op.create_check_constraint("event_type_allowed", "web_push_events", "event_type IN ('inbound_message', 'automatic_booking')")
    op.drop_constraint("whatsapp_review_status_allowed", "businesses", type_="check")
    op.drop_constraint("whatsapp_setup_source_allowed", "businesses", type_="check")
    op.drop_constraint("whatsapp_desired_mode_allowed", "businesses", type_="check")
    op.drop_column("businesses", "whatsapp_review_notified_at")
    op.drop_column("businesses", "whatsapp_review_checked_at")
    op.drop_column("businesses", "whatsapp_review_status")
    op.drop_column("businesses", "whatsapp_setup_source")
    op.drop_column("businesses", "whatsapp_desired_mode")

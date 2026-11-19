"""Add MCP OAuth pending authorization flows.

Revision ID: 20261118_0046
Revises: 20261118_0045
Create Date: 2026-11-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261118_0046"
down_revision = "20261118_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_authorization_flows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "flow_token_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column(
            "redirect_uri",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column(
            "code_challenge",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "scope",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "resource",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
            "flow_token_hash ~ '^[0-9a-f]{64}$'",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "flow_token_hash_format"
            ),
        ),
        sa.CheckConstraint(
            "length(state) > 0",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "state_not_blank"
            ),
        ),
        sa.CheckConstraint(
            "code_challenge ~ "
            "'^[A-Za-z0-9_-]{43,128}$'",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "code_challenge_format"
            ),
        ),
        sa.CheckConstraint(
            "length(trim(scope)) > 0",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "scope_not_blank"
            ),
        ),
        sa.CheckConstraint(
            "resource LIKE 'https://%'",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "resource_https"
            ),
        ),
        sa.CheckConstraint(
            "redirect_uri LIKE 'https://%'",
            name=(
                "ck_mcp_oauth_authorization_flows_"
                "redirect_uri_https"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=(
                "fk_mcp_oauth_authorization_flows_"
                "user_id_users"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=(
                "fk_mcp_oauth_authorization_flows_"
                "business_id_businesses"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_mcp_oauth_authorization_flows",
        ),
        sa.UniqueConstraint(
            "flow_token_hash",
            name=(
                "uq_mcp_oauth_authorization_flows_"
                "flow_token_hash"
            ),
        ),
    )

    op.create_index(
        "ix_mcp_oauth_authorization_flows_expires_at",
        "mcp_oauth_authorization_flows",
        ["expires_at"],
    )
    op.create_index(
        "ix_mcp_oauth_authorization_flows_user_id",
        "mcp_oauth_authorization_flows",
        ["user_id"],
    )

    op.execute(
        'ALTER TABLE "mcp_oauth_authorization_flows" '
        "ENABLE ROW LEVEL SECURITY"
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE "mcp_oauth_authorization_flows" '
        "DISABLE ROW LEVEL SECURITY"
    )

    op.drop_index(
        "ix_mcp_oauth_authorization_flows_user_id",
        table_name="mcp_oauth_authorization_flows",
    )
    op.drop_index(
        "ix_mcp_oauth_authorization_flows_expires_at",
        table_name="mcp_oauth_authorization_flows",
    )

    op.drop_table(
        "mcp_oauth_authorization_flows"
    )

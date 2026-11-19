"""Add isolated MCP OAuth persistence foundation.

Revision ID: 20261118_0045
Revises: 20261111_0044
Create Date: 2026-11-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261118_0045"
down_revision = "20261111_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_authorization_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
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
            "code_hash",
            sa.String(length=64),
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
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
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
            "code_hash ~ '^[0-9a-f]{64}$'",
            name=(
                "ck_mcp_oauth_authorization_codes_"
                "code_hash_format"
            ),
        ),
        sa.CheckConstraint(
            "code_challenge ~ "
            "'^[A-Za-z0-9_-]{43,128}$'",
            name=(
                "ck_mcp_oauth_authorization_codes_"
                "code_challenge_format"
            ),
        ),
        sa.CheckConstraint(
            "length(trim(scope)) > 0",
            name=(
                "ck_mcp_oauth_authorization_codes_"
                "scope_not_blank"
            ),
        ),
        sa.CheckConstraint(
            "resource LIKE 'https://%'",
            name=(
                "ck_mcp_oauth_authorization_codes_"
                "resource_https"
            ),
        ),
        sa.CheckConstraint(
            "redirect_uri LIKE 'https://%'",
            name=(
                "ck_mcp_oauth_authorization_codes_"
                "redirect_uri_https"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=(
                "fk_mcp_oauth_authorization_codes_"
                "user_id_users"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=(
                "fk_mcp_oauth_authorization_codes_"
                "business_id_businesses"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_mcp_oauth_authorization_codes",
        ),
        sa.UniqueConstraint(
            "code_hash",
            name=(
                "uq_mcp_oauth_authorization_codes_"
                "code_hash"
            ),
        ),
    )

    op.create_index(
        "ix_mcp_oauth_authorization_codes_expires_at",
        "mcp_oauth_authorization_codes",
        ["expires_at"],
    )
    op.create_index(
        "ix_mcp_oauth_authorization_codes_business_id",
        "mcp_oauth_authorization_codes",
        ["business_id"],
    )

    op.create_table(
        "mcp_oauth_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column(
            "refresh_token_hash",
            sa.String(length=64),
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
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
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
            "refresh_token_hash ~ '^[0-9a-f]{64}$'",
            name=(
                "ck_mcp_oauth_sessions_"
                "refresh_token_hash_format"
            ),
        ),
        sa.CheckConstraint(
            "length(trim(scope)) > 0",
            name="ck_mcp_oauth_sessions_scope_not_blank",
        ),
        sa.CheckConstraint(
            "resource LIKE 'https://%'",
            name="ck_mcp_oauth_sessions_resource_https",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_mcp_oauth_sessions_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=(
                "fk_mcp_oauth_sessions_"
                "business_id_businesses"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_mcp_oauth_sessions",
        ),
        sa.UniqueConstraint(
            "refresh_token_hash",
            name=(
                "uq_mcp_oauth_sessions_"
                "refresh_token_hash"
            ),
        ),
    )

    op.create_index(
        "ix_mcp_oauth_sessions_expires_at",
        "mcp_oauth_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_mcp_oauth_sessions_business_id",
        "mcp_oauth_sessions",
        ["business_id"],
    )
    op.create_index(
        "ix_mcp_oauth_sessions_user_id",
        "mcp_oauth_sessions",
        ["user_id"],
    )

    op.execute(
        'ALTER TABLE "mcp_oauth_authorization_codes" '
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        'ALTER TABLE "mcp_oauth_sessions" '
        "ENABLE ROW LEVEL SECURITY"
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE "mcp_oauth_sessions" '
        "DISABLE ROW LEVEL SECURITY"
    )
    op.execute(
        'ALTER TABLE "mcp_oauth_authorization_codes" '
        "DISABLE ROW LEVEL SECURITY"
    )

    op.drop_index(
        "ix_mcp_oauth_sessions_user_id",
        table_name="mcp_oauth_sessions",
    )
    op.drop_index(
        "ix_mcp_oauth_sessions_business_id",
        table_name="mcp_oauth_sessions",
    )
    op.drop_index(
        "ix_mcp_oauth_sessions_expires_at",
        table_name="mcp_oauth_sessions",
    )
    op.drop_table("mcp_oauth_sessions")

    op.drop_index(
        "ix_mcp_oauth_authorization_codes_business_id",
        table_name="mcp_oauth_authorization_codes",
    )
    op.drop_index(
        "ix_mcp_oauth_authorization_codes_expires_at",
        table_name="mcp_oauth_authorization_codes",
    )
    op.drop_table("mcp_oauth_authorization_codes")

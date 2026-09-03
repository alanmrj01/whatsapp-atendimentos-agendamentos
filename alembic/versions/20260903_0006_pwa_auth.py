"""Add SaaS users, memberships and revocable sessions (no seed data)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260903_0006"
down_revision = "20260902_0005"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("platform_role", sa.String(20), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("email = lower(trim(email)) AND length(email) > 3", name=op.f("ck_users_email_normalized")),
        sa.CheckConstraint("platform_role IS NULL OR platform_role = 'super_admin'", name=op.f("ck_users_platform_role_allowed")),
        sa.CheckConstraint("password_hash LIKE '$argon2id$%'", name=op.f("ck_users_password_hash_argon2id")),
    )
    op.create_table(
        "business_user_memberships",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "business_id", name=op.f("pk_business_user_memberships")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_business_user_memberships_user_id_users")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], name=op.f("fk_business_user_memberships_business_id_businesses")),
        sa.CheckConstraint("role IN ('owner', 'admin', 'attendant', 'viewer')", name=op.f("ck_business_user_memberships_role_allowed")),
    )
    op.create_index("ix_business_user_memberships_business_id", "business_user_memberships", ["business_id"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active_business_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users")),
        sa.ForeignKeyConstraint(["active_business_id"], ["businesses.id"], name=op.f("fk_auth_sessions_active_business_id_businesses")),
        sa.UniqueConstraint("refresh_token_hash", name=op.f("uq_auth_sessions_refresh_token_hash")),
        sa.CheckConstraint("refresh_token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_auth_sessions_refresh_hash_format")),
    )
    for column in ("user_id", "active_business_id", "expires_at"):
        op.create_index(f"ix_auth_sessions_{column}", "auth_sessions", [column])


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("business_user_memberships")
    op.drop_table("users")

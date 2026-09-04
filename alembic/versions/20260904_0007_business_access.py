"""Add business access mode for free demonstrations and paid operations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260904_0007"
down_revision = "20260903_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_access",
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_mode", sa.String(16), nullable=False, server_default=sa.text("'free'")),
        sa.PrimaryKeyConstraint("business_id", name=op.f("pk_business_access")),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_business_access_business_id_businesses"),
        ),
        sa.CheckConstraint(
            "access_mode IN ('free', 'paid')",
            name=op.f("ck_business_access_access_mode_allowed"),
        ),
    )
    # Preserve all tenants that already existed before the free-preview flow.
    # New public signups explicitly create a `free` row.
    op.execute(
        "INSERT INTO business_access (business_id, access_mode) "
        "SELECT id, 'paid' FROM businesses"
    )


def downgrade() -> None:
    op.drop_table("business_access")

"""Support pending operational appointments and private notes."""

from alembic import op
import sqlalchemy as sa

revision = "20260908_0008"
down_revision = "20260904_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("notes", sa.Text(), nullable=True))
    op.drop_constraint(
        op.f("ck_appointments_status_allowed"),
        "appointments",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_appointments_status_allowed"),
        "appointments",
        "status IN ('pending', 'confirmed', 'cancelled', 'completed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_appointments_status_allowed"),
        "appointments",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_appointments_status_allowed"),
        "appointments",
        "status IN ('confirmed', 'cancelled', 'completed')",
    )
    op.drop_column("appointments", "notes")

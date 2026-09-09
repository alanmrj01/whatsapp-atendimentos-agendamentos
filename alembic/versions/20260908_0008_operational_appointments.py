"""Support pending operational appointments and private notes."""

from alembic import op
import sqlalchemy as sa

revision = "20260908_0008"
down_revision = "20260904_0007"
branch_labels = None
depends_on = None

_ROLLBACK_KEY = "__alovia_migration_20260908_0008"


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

    # A previous downgrade can temporarily encode fields introduced by this
    # migration inside the pre-existing JSONB column. Restore them only after
    # the wider status constraint and notes column exist again.
    op.execute(
        sa.text(
            f"""
            UPDATE appointments
            SET
                status = CASE
                    WHEN estimate_details -> '{_ROLLBACK_KEY}' ->> 'migration' = '{revision}'
                         AND estimate_details -> '{_ROLLBACK_KEY}' ->> 'status' = 'pending'
                    THEN 'pending'
                    ELSE status
                END,
                notes = CASE
                    WHEN estimate_details -> '{_ROLLBACK_KEY}' ->> 'migration' = '{revision}'
                    THEN estimate_details -> '{_ROLLBACK_KEY}' ->> 'notes'
                    ELSE notes
                END,
                estimate_details = estimate_details - '{_ROLLBACK_KEY}'
            WHERE estimate_details -> '{_ROLLBACK_KEY}' ->> 'migration' = '{revision}'
            """
        )
    )


def downgrade() -> None:
    # Version 0007 cannot represent pending or notes. Preserve both inside the
    # already-existing estimate_details JSONB so a later re-upgrade can restore
    # them exactly. Refuse to overwrite a pre-existing reserved key.
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM appointments
                    WHERE estimate_details ? '{_ROLLBACK_KEY}'
                ) THEN
                    RAISE EXCEPTION
                        'reserved rollback key {_ROLLBACK_KEY} already exists';
                END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE appointments
            SET estimate_details = estimate_details || jsonb_build_object(
                '{_ROLLBACK_KEY}',
                jsonb_build_object(
                    'migration', '{revision}',
                    'status', status,
                    'notes', notes
                )
            )
            WHERE status = 'pending' OR notes IS NOT NULL
            """
        )
    )

    # Use cancelled as the temporary 0007-compatible value. Unlike confirmed,
    # it cannot trigger the confirmed-appointment exclusion constraint when a
    # pending slot overlaps an existing confirmed appointment.
    op.execute("UPDATE appointments SET status = 'cancelled' WHERE status = 'pending'")

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

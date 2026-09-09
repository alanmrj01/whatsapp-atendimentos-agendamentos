from pathlib import Path

from tests.test_migration import render_migration_sql


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260908_0008_operational_appointments.py"
)
ROLLBACK_KEY = "__alovia_migration_20260908_0008"


def _normalized(direction: str) -> str:
    return " ".join(render_migration_sql(direction, MIGRATION_PATH).lower().split())


def test_0008_offline_sql_preserves_new_data_across_downgrade_and_reupgrade() -> None:
    upgrade = _normalized("upgrade")
    downgrade = _normalized("downgrade")

    assert "add column notes text" in upgrade
    assert "status in ('pending', 'confirmed', 'cancelled', 'completed')" in upgrade
    assert ROLLBACK_KEY in upgrade
    assert "then 'pending'" in upgrade
    assert f"estimate_details = estimate_details - '{ROLLBACK_KEY}'" in upgrade

    assert ROLLBACK_KEY in downgrade
    assert "jsonb_build_object" in downgrade
    assert "where status = 'pending' or notes is not null" in downgrade
    assert "set status = 'cancelled' where status = 'pending'" in downgrade
    assert "status in ('confirmed', 'cancelled', 'completed')" in downgrade
    assert "drop column notes" in downgrade

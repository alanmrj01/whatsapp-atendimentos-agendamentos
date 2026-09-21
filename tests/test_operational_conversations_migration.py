from alembic.config import Config
from alembic.script import ScriptDirectory

from app.operations.defaults import DEFAULT_OPERATIONAL_SERVICES
from tests.test_migration import PROJECT_ROOT, render_migration_sql


MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260921_0011_operational_conversations.py"
)


def test_operational_conversations_migration_is_head_after_restored_0010() -> None:
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    assert script.get_heads() == ["20260921_0012"]
    assert script.get_revision("20260921_0012").down_revision == "20260921_0011"
    assert script.get_revision("20260921_0011").down_revision == "20260915_0010"
    assert script.get_revision("20260915_0010").down_revision == "20260915_0009"


def test_backfill_adds_all_five_defaults_only_to_empty_businesses() -> None:
    upgrade = " ".join(render_migration_sql("upgrade", MIGRATION_PATH).split())
    lowered = upgrade.casefold()

    assert "with empty_businesses as materialized" in lowered
    assert "where not exists" in lowered
    assert "cross join defaults" in lowered
    assert "update services" not in lowered
    assert "delete from services" not in lowered
    for item in DEFAULT_OPERATIONAL_SERVICES:
        assert item.key in upgrade
        assert item.name in upgrade
        assert str(item.duration_minutes) in upgrade
    assert "null, 'estimated', true" in lowered


def test_migration_downgrade_is_schema_only_and_preserves_service_data() -> None:
    downgrade = " ".join(
        render_migration_sql("downgrade", MIGRATION_PATH).casefold().split()
    )

    assert "drop column whatsapp_profile_name" in downgrade
    assert "drop column operational_role" in downgrade
    assert "drop column assistant_enabled" in downgrade
    assert "delete from services" not in downgrade
    assert "drop table services" not in downgrade

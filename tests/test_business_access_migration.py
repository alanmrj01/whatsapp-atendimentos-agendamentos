from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.test_migration import PROJECT_ROOT, render_migration_sql


BUSINESS_ACCESS_MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260904_0007_business_access.py"
)
ACCESS_HISTORY_MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260915_0009_access_history.py"
)


def test_access_history_remains_in_current_alembic_chain() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20260922_0014"]
    assert script.get_revision("20260915_0009").down_revision == "20260908_0008"


def test_business_access_migration_preserves_existing_tenants_as_paid() -> None:
    upgrade = " ".join(render_migration_sql("upgrade", BUSINESS_ACCESS_MIGRATION_PATH).lower().split())
    downgrade = " ".join(render_migration_sql("downgrade", BUSINESS_ACCESS_MIGRATION_PATH).lower().split())

    assert "create table business_access" in upgrade
    assert "foreign key(business_id) references businesses (id)" in upgrade
    assert "access_mode varchar(16) default 'free' not null" in upgrade
    assert "access_mode in ('free', 'paid')" in upgrade
    assert "insert into business_access (business_id, access_mode) select id, 'paid' from businesses" in upgrade
    assert "drop table business_access" in downgrade


def test_access_history_migration_is_additive_and_never_deletes_business_data() -> None:
    upgrade = " ".join(render_migration_sql("upgrade", ACCESS_HISTORY_MIGRATION_PATH).lower().split())
    downgrade = " ".join(render_migration_sql("downgrade", ACCESS_HISTORY_MIGRATION_PATH).lower().split())

    assert "add column has_had_operational_access boolean default false not null" in upgrade
    assert "set has_had_operational_access = true" in upgrade
    assert "access_mode = 'paid'" in upgrade
    assert "business_whatsapp_connections" in upgrade
    assert "conversations" in upgrade
    assert "appointments" in upgrade
    assert "customers" in upgrade
    assert "delete from" not in upgrade
    assert "drop table" not in upgrade
    assert "drop column has_had_operational_access" in downgrade

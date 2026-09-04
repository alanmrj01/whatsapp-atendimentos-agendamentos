from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.test_migration import PROJECT_ROOT, render_migration_sql


MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260904_0007_business_access.py"
)


def test_business_access_is_current_alembic_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20260904_0007"]


def test_business_access_migration_preserves_existing_tenants_as_paid() -> None:
    upgrade = " ".join(render_migration_sql("upgrade", MIGRATION_PATH).lower().split())
    downgrade = " ".join(render_migration_sql("downgrade", MIGRATION_PATH).lower().split())

    assert "create table business_access" in upgrade
    assert "foreign key(business_id) references businesses (id)" in upgrade
    assert "access_mode varchar(16) default 'free' not null" in upgrade
    assert "access_mode in ('free', 'paid')" in upgrade
    assert "insert into business_access (business_id, access_mode) select id, 'paid' from businesses" in upgrade
    assert "drop table business_access" in downgrade

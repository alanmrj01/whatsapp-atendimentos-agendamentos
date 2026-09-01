from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from types import ModuleType

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "20260901_0001_initial_schema.py"
)


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "initial_schema_migration", MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_migration_sql(direction: str) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration = load_migration()
    migration.op = Operations(context)
    getattr(migration, direction)()
    return output.getvalue()


def test_initial_migration_is_the_only_alembic_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20260901_0001"]


def test_upgrade_sql_creates_all_tables_and_postgresql_constraints() -> None:
    sql = render_migration_sql("upgrade")
    normalized = " ".join(sql.split()).lower()

    assert "create extension if not exists btree_gist" in normalized
    for table_name in (
        "businesses",
        "customers",
        "conversations",
        "services",
        "employees",
        "employee_services",
        "working_hours",
        "schedule_blocks",
        "appointments",
        "messages",
        "processed_webhooks",
    ):
        assert f"create table {table_name}" in normalized

    assert "exclude using gist" in normalized
    assert "tstzrange(starts_at, ends_at, '[)') with &&" in normalized
    assert "where (status = 'confirmed')" in normalized
    assert "check (weekday between 0 and 6)" in normalized
    assert "check (ends_at > starts_at)" in normalized
    assert "where provider_message_id is not null" in normalized
    assert "where idempotency_key is not null" in normalized


def test_downgrade_sql_drops_schema_in_dependency_safe_order() -> None:
    sql = render_migration_sql("downgrade").lower()

    table_positions = [
        sql.index(f"drop table {table_name}")
        for table_name in (
            "processed_webhooks",
            "messages",
            "appointments",
            "schedule_blocks",
            "working_hours",
            "employee_services",
            "employees",
            "services",
            "conversations",
            "customers",
            "businesses",
        )
    ]
    assert table_positions == sorted(table_positions)
    assert "drop extension" not in sql

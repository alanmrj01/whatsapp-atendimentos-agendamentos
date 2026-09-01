from __future__ import annotations

import importlib.util
import hashlib
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
OUTBOUND_PAYLOAD_MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260901_0002_add_outbound_payload.py"
)
BOOKING_MIGRATION_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "20260901_0003_booking_configuration.py"
)


def load_migration(path: Path = MIGRATION_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"migration_{path.stem}", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_migration_sql(
    direction: str,
    path: Path = MIGRATION_PATH,
) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration = load_migration(path)
    migration.op = Operations(context)
    getattr(migration, direction)()
    return output.getvalue()


def test_booking_configuration_migration_is_the_only_alembic_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["20260901_0003"]


def test_previous_migrations_remain_byte_identical() -> None:
    expected = {
        MIGRATION_PATH: (
            "cf5f5686ab0b8381ee4e1a0ef7a09a0c9e066a119f272b235a1e69e6356872f6"
        ),
        OUTBOUND_PAYLOAD_MIGRATION_PATH: (
            "7a8c91faccb6c6131be3f596a7f64dda424021477708e002b7c64e7df0b5ded3"
        ),
    }

    for path, digest in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_booking_configuration_upgrade_and_downgrade_sql() -> None:
    upgrade = " ".join(
        render_migration_sql("upgrade", BOOKING_MIGRATION_PATH).lower().split()
    )
    downgrade = " ".join(
        render_migration_sql("downgrade", BOOKING_MIGRATION_PATH).lower().split()
    )

    for column_name in (
        "service_origin_address",
        "default_travel_minutes",
        "pricing_type",
        "automatic_booking",
        "base_price",
        "estimated_duration_minutes",
        "travel_before_minutes",
        "travel_after_minutes",
        "service_address",
        "idempotency_key",
    ):
        assert column_name in upgrade
        assert f"drop column {column_name}" in downgrade
    assert "make_interval(mins => travel_before_minutes)" in upgrade
    assert "make_interval(mins => travel_after_minutes)" in upgrade
    assert "uq_appointments_idempotency_key_present" in upgrade
    assert "tstzrange(starts_at, ends_at, '[)')" in downgrade


def test_outbound_payload_migration_upgrade_and_downgrade_sql() -> None:
    upgrade_sql = render_migration_sql(
        "upgrade",
        OUTBOUND_PAYLOAD_MIGRATION_PATH,
    ).lower()
    downgrade_sql = render_migration_sql(
        "downgrade",
        OUTBOUND_PAYLOAD_MIGRATION_PATH,
    ).lower()

    assert "alter table messages add column outbound_payload jsonb" in " ".join(
        upgrade_sql.split()
    )
    assert "alter table messages drop column outbound_payload" in " ".join(
        downgrade_sql.split()
    )


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
    for constraint_name, foreign_key_sql in {
        "fk_conversations_business_customer_customers": (
            "foreign key(business_id, customer_id) "
            "references customers (business_id, id)"
        ),
        "fk_employee_services_business_employee_employees": (
            "foreign key(business_id, employee_id) "
            "references employees (business_id, id)"
        ),
        "fk_employee_services_business_id_businesses": (
            "foreign key(business_id) references businesses (id)"
        ),
        "fk_employee_services_business_service_services": (
            "foreign key(business_id, service_id) "
            "references services (business_id, id)"
        ),
        "fk_working_hours_business_employee_employees": (
            "foreign key(business_id, employee_id) "
            "references employees (business_id, id)"
        ),
        "fk_schedule_blocks_business_employee_employees": (
            "foreign key(business_id, employee_id) "
            "references employees (business_id, id)"
        ),
        "fk_appointments_business_customer_customers": (
            "foreign key(business_id, customer_id) "
            "references customers (business_id, id)"
        ),
        "fk_appointments_business_service_services": (
            "foreign key(business_id, service_id) "
            "references services (business_id, id)"
        ),
        "fk_appointments_business_employee_employees": (
            "foreign key(business_id, employee_id) "
            "references employees (business_id, id)"
        ),
        "fk_messages_business_conversation_conversations": (
            "foreign key(business_id, conversation_id) "
            "references conversations (business_id, id)"
        ),
    }.items():
        assert f"constraint {constraint_name}" in normalized
        assert foreign_key_sql in normalized

    for constraint_name, unique_sql in {
        "uq_customers_business_id_id": "unique (business_id, id)",
        "uq_services_business_id_id": "unique (business_id, id)",
        "uq_employees_business_id_id": "unique (business_id, id)",
        "uq_conversations_business_id_id": "unique (business_id, id)",
    }.items():
        assert f"constraint {constraint_name} {unique_sql}" in normalized

    employee_services_sql = normalized.split(
        "create table employee_services", maxsplit=1
    )[1].split("create index", maxsplit=1)[0]
    assert "business_id uuid not null" in employee_services_sql
    assert "ix_employee_services_business_id" in normalized


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

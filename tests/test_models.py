from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.schema import CreateTable

from app.models import Base


EXPECTED_TABLES = {
    "appointments",
    "businesses",
    "conversations",
    "customers",
    "employee_services",
    "employees",
    "messages",
    "processed_webhooks",
    "schedule_blocks",
    "services",
    "working_hours",
}


def test_all_domain_tables_are_registered_in_metadata() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_foreign_keys_do_not_enable_destructive_cascades() -> None:
    foreign_keys = [
        foreign_key
        for table in Base.metadata.tables.values()
        for foreign_key in table.foreign_keys
    ]

    assert foreign_keys
    assert all(foreign_key.ondelete is None for foreign_key in foreign_keys)


def test_business_and_service_positive_duration_constraints() -> None:
    constraint_names = {
        constraint.name
        for table_name in ("businesses", "services")
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert constraint_names == {
        "ck_businesses_slot_interval_minutes_positive",
        "ck_services_duration_minutes_positive",
    }


def test_working_hours_and_appointment_constraints_are_registered() -> None:
    working_hours_checks = {
        constraint.name
        for constraint in Base.metadata.tables["working_hours"].constraints
        if isinstance(constraint, CheckConstraint)
    }
    appointment_checks = {
        constraint.name
        for constraint in Base.metadata.tables["appointments"].constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert working_hours_checks == {
        "ck_working_hours_weekday_range",
        "ck_working_hours_end_time_after_start_time",
    }
    assert appointment_checks == {
        "ck_appointments_ends_at_after_starts_at",
        "ck_appointments_status_allowed",
    }


def test_schedule_block_and_message_constraints_are_registered() -> None:
    schedule_block_checks = {
        constraint.name
        for constraint in Base.metadata.tables["schedule_blocks"].constraints
        if isinstance(constraint, CheckConstraint)
    }
    message_checks = {
        constraint.name
        for constraint in Base.metadata.tables["messages"].constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert schedule_block_checks == {
        "ck_schedule_blocks_ends_at_after_starts_at"
    }
    assert message_checks == {"ck_messages_direction_allowed"}


def test_business_scoped_uniques_and_composite_primary_key() -> None:
    customer_uniques = {
        constraint.name
        for constraint in Base.metadata.tables["customers"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    conversation_uniques = {
        constraint.name
        for constraint in Base.metadata.tables["conversations"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    employee_service_pk = Base.metadata.tables["employee_services"].primary_key

    assert customer_uniques == {"uq_customers_business_whatsapp"}
    assert conversation_uniques == {"uq_conversations_business_customer"}
    assert [column.name for column in employee_service_pk.columns] == [
        "employee_id",
        "service_id",
    ]


def test_external_business_and_webhook_ids_are_unique() -> None:
    business_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["businesses"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    webhook_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["processed_webhooks"].constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("meta_phone_number_id",) in business_uniques
    assert webhook_uniques == {("event_key",)}


def test_conversation_context_uses_jsonb_with_empty_object_default() -> None:
    context_column = Base.metadata.tables["conversations"].c.context

    assert isinstance(context_column.type, JSONB)
    assert str(context_column.server_default.arg) == "'{}'::jsonb"


def test_required_server_defaults_are_registered() -> None:
    expected_defaults = {
        ("businesses", "timezone"): "America/Sao_Paulo",
        ("businesses", "slot_interval_minutes"): "30",
        ("businesses", "active"): "true",
        ("conversations", "automation_enabled"): "true",
        ("conversations", "handoff_status"): "none",
        ("processed_webhooks", "attempts"): "0",
    }

    for (table_name, column_name), expected_default in expected_defaults.items():
        column = Base.metadata.tables[table_name].c[column_name]
        assert str(column.server_default.arg) == expected_default


def test_confirmed_appointments_have_overlap_exclusion_constraint() -> None:
    exclusion_constraints = [
        constraint
        for constraint in Base.metadata.tables["appointments"].constraints
        if isinstance(constraint, ExcludeConstraint)
    ]

    assert len(exclusion_constraints) == 1
    exclusion = exclusion_constraints[0]
    assert exclusion.name == "excl_appointments_employee_confirmed_overlap"
    assert exclusion.using == "gist"
    assert str(exclusion.where) == "status = 'confirmed'"
    table_sql = str(
        CreateTable(Base.metadata.tables["appointments"]).compile(
            dialect=postgresql_dialect()
        )
    )
    assert "employee_id WITH =" in table_sql
    assert "tstzrange(starts_at, ends_at, '[)') WITH &&" in table_sql


def test_message_identifiers_use_partial_unique_indexes() -> None:
    indexes = {
        index.name: index for index in Base.metadata.tables["messages"].indexes
    }

    for index_name, predicate in (
        (
            "uq_messages_provider_message_id_present",
            "provider_message_id IS NOT NULL",
        ),
        ("uq_messages_idempotency_key_present", "idempotency_key IS NOT NULL"),
    ):
        index = indexes[index_name]
        assert index.unique is True
        assert str(index.dialect_options["postgresql"]["where"]) == predicate


def test_status_and_date_indexes_are_present() -> None:
    expected_indexes = {
        "appointments": {
            "ix_appointments_employee_starts_at",
            "ix_appointments_status",
            "ix_appointments_starts_at",
        },
        "conversations": {
            "ix_conversations_handoff_status",
            "ix_conversations_last_interaction_at",
            "ix_conversations_state",
        },
        "messages": {"ix_messages_created_at", "ix_messages_status"},
        "processed_webhooks": {
            "ix_processed_webhooks_received_at",
            "ix_processed_webhooks_status",
        },
    }

    for table_name, index_names in expected_indexes.items():
        actual_names = {
            index.name for index in Base.metadata.tables[table_name].indexes
        }
        assert index_names <= actual_names

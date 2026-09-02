from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.schema import CreateTable

from app.models import Base


EXPECTED_TABLES = {
    "appointments",
    "businesses",
    "business_automation_exclusions",
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


def foreign_key_specs(
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    return {
        constraint.name: (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in Base.metadata.tables[table_name].foreign_key_constraints
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

    assert {
        "ck_businesses_slot_interval_minutes_positive",
        "ck_services_duration_minutes_positive",
    } <= constraint_names


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
    assert {
        "ck_appointments_ends_at_after_starts_at",
        "ck_appointments_status_allowed",
        "ck_appointments_estimated_duration_minutes_positive",
        "ck_appointments_travel_before_minutes_nonnegative",
        "ck_appointments_travel_after_minutes_nonnegative",
    } <= appointment_checks


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


def test_business_scoped_uniques_support_composite_foreign_keys() -> None:
    unique_specs = {
        table_name: {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in Base.metadata.tables[table_name].constraints
            if isinstance(constraint, UniqueConstraint)
        }
        for table_name in ("customers", "services", "employees", "conversations")
    }

    assert unique_specs["customers"] == {
        "uq_customers_business_whatsapp": ("business_id", "whatsapp_id"),
        "uq_customers_business_id_id": ("business_id", "id"),
    }
    assert unique_specs["services"] == {
        "uq_services_business_id_id": ("business_id", "id")
    }
    assert unique_specs["employees"] == {
        "uq_employees_business_id_id": ("business_id", "id")
    }
    assert unique_specs["conversations"] == {
        "uq_conversations_business_customer": ("business_id", "customer_id"),
        "uq_conversations_business_id_id": ("business_id", "id"),
    }


def test_employee_services_preserves_pair_primary_key_and_business_index() -> None:
    table = Base.metadata.tables["employee_services"]
    employee_service_pk = Base.metadata.tables["employee_services"].primary_key

    assert [column.name for column in employee_service_pk.columns] == [
        "employee_id",
        "service_id",
    ]
    assert table.c.business_id.nullable is False
    assert "ix_employee_services_business_id" in {
        index.name for index in table.indexes
    }


def test_cross_business_relationships_use_composite_foreign_keys() -> None:
    expected_specs = {
        "conversations": {
            "fk_conversations_business_customer_customers": (
                ("business_id", "customer_id"),
                ("customers.business_id", "customers.id"),
            )
        },
        "working_hours": {
            "fk_working_hours_business_employee_employees": (
                ("business_id", "employee_id"),
                ("employees.business_id", "employees.id"),
            )
        },
        "schedule_blocks": {
            "fk_schedule_blocks_business_employee_employees": (
                ("business_id", "employee_id"),
                ("employees.business_id", "employees.id"),
            )
        },
        "appointments": {
            "fk_appointments_business_customer_customers": (
                ("business_id", "customer_id"),
                ("customers.business_id", "customers.id"),
            ),
            "fk_appointments_business_service_services": (
                ("business_id", "service_id"),
                ("services.business_id", "services.id"),
            ),
            "fk_appointments_business_employee_employees": (
                ("business_id", "employee_id"),
                ("employees.business_id", "employees.id"),
            ),
        },
        "messages": {
            "fk_messages_business_conversation_conversations": (
                ("business_id", "conversation_id"),
                ("conversations.business_id", "conversations.id"),
            )
        },
    }

    for table_name, expected in expected_specs.items():
        assert foreign_key_specs(table_name) == expected


def test_employee_services_references_business_employee_and_service() -> None:
    assert foreign_key_specs("employee_services") == {
        "fk_employee_services_business_id_businesses": (
            ("business_id",),
            ("businesses.id",),
        ),
        "fk_employee_services_business_employee_employees": (
            ("business_id", "employee_id"),
            ("employees.business_id", "employees.id"),
        ),
        "fk_employee_services_business_service_services": (
            ("business_id", "service_id"),
            ("services.business_id", "services.id"),
        ),
    }


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


def test_message_outbound_payload_is_nullable_jsonb() -> None:
    outbound_payload = Base.metadata.tables["messages"].c.outbound_payload

    assert isinstance(outbound_payload.type, JSONB)
    assert outbound_payload.nullable is True


def test_required_server_defaults_are_registered() -> None:
    expected_defaults = {
        ("businesses", "timezone"): "America/Sao_Paulo",
        ("businesses", "slot_interval_minutes"): "30",
        ("businesses", "human_control_window_minutes"): "2160",
        ("businesses", "service_origin_address"): (
            "Zona Leste de São José dos Campos - SP"
        ),
        ("businesses", "service_origin_is_precise"): "false",
        ("businesses", "travel_calculation_method"): "configured_estimate",
        ("businesses", "travel_fallback_allowed"): "false",
        ("businesses", "travel_before_buffer_minutes"): "0",
        ("businesses", "travel_after_buffer_minutes"): "0",
        ("businesses", "active"): "true",
        ("services", "pricing_type"): "estimated",
        ("services", "automatic_booking"): "true",
        ("services", "included_quantity"): "1",
        ("conversations", "automation_enabled"): "true",
        ("conversations", "handoff_status"): "none",
        ("processed_webhooks", "attempts"): "0",
    }

    for (table_name, column_name), expected_default in expected_defaults.items():
        column = Base.metadata.tables[table_name].c[column_name]
        assert str(column.server_default.arg) == expected_default

    default_travel = Base.metadata.tables["businesses"].c.default_travel_minutes
    assert default_travel.server_default is None
    assert default_travel.nullable is True


def test_automation_exclusions_and_human_control_are_registered() -> None:
    exclusions = Base.metadata.tables["business_automation_exclusions"]
    businesses = Base.metadata.tables["businesses"]
    conversations = Base.metadata.tables["conversations"]

    unique_specs = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in exclusions.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_specs == {
        "uq_business_automation_exclusions_business_whatsapp": (
            "business_id",
            "whatsapp_id",
        )
    }
    assert "ix_business_automation_exclusions_lookup" in {
        index.name for index in exclusions.indexes
    }
    assert exclusions.c.business_id.references(businesses.c.id)
    assert {
        "automation_suppressed_until",
        "suppression_reason",
        "human_control_started_at",
        "last_human_message_at",
        "conversation_initiated_by",
    } <= set(conversations.c.keys())
    assert "ix_conversations_automation_suppressed_until" in {
        index.name for index in conversations.indexes
    }

    business_checks = {
        constraint.name
        for constraint in businesses.constraints
        if isinstance(constraint, CheckConstraint)
    }
    exclusion_checks = {
        constraint.name
        for constraint in exclusions.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_businesses_human_control_window_minutes_allowed" in business_checks
    assert exclusion_checks == {
        "ck_business_automation_exclusions_mode_allowed",
        "ck_business_automation_exclusions_whatsapp_id_normalized",
    }


def test_business_travel_configuration_constraints_are_registered() -> None:
    constraints = {
        constraint.name
        for constraint in Base.metadata.tables["businesses"].constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {
        "ck_businesses_default_travel_minutes_nonnegative",
        "ck_businesses_travel_calculation_method_allowed",
        "ck_businesses_travel_fallback_requires_minutes",
        "ck_businesses_service_origin_coordinates_together",
        "ck_businesses_service_origin_latitude_range",
        "ck_businesses_service_origin_longitude_range",
    } <= constraints


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
    assert "booking_add_minutes_immutable" in table_sql
    assert "starts_at, -travel_before_minutes" in table_sql
    assert "ends_at, travel_after_minutes" in table_sql


def test_appointment_booking_snapshot_and_idempotency_index() -> None:
    table = Base.metadata.tables["appointments"]

    for column_name in (
        "service_address",
        "quantity",
        "access_condition",
        "estimated_duration_minutes",
        "travel_before_minutes",
        "travel_after_minutes",
        "estimated_price",
        "pricing_type",
        "estimate_details",
        "site_allowed_end",
        "idempotency_key",
    ):
        assert column_name in table.c
    index = next(
        value
        for value in table.indexes
        if value.name == "uq_appointments_idempotency_key_present"
    )
    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "idempotency_key IS NOT NULL"
    )


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

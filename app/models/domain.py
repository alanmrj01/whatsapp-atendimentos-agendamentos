from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Business(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "businesses"
    __table_args__ = (
        CheckConstraint(
            "slot_interval_minutes > 0", name="slot_interval_minutes_positive"
        ),
        CheckConstraint(
            "default_travel_minutes IS NULL OR default_travel_minutes >= 0",
            name="default_travel_minutes_nonnegative",
        ),
        CheckConstraint(
            "travel_calculation_method IN ('route', 'configured_estimate')",
            name="travel_calculation_method_allowed",
        ),
        CheckConstraint(
            "NOT travel_fallback_allowed OR default_travel_minutes IS NOT NULL",
            name="travel_fallback_requires_minutes",
        ),
        CheckConstraint(
            "(service_origin_latitude IS NULL) = "
            "(service_origin_longitude IS NULL)",
            name="service_origin_coordinates_together",
        ),
        CheckConstraint(
            "service_origin_latitude IS NULL OR "
            "service_origin_latitude BETWEEN -90 AND 90",
            name="service_origin_latitude_range",
        ),
        CheckConstraint(
            "service_origin_longitude IS NULL OR "
            "service_origin_longitude BETWEEN -180 AND 180",
            name="service_origin_longitude_range",
        ),
        CheckConstraint(
            "travel_before_buffer_minutes >= 0",
            name="travel_before_buffer_minutes_nonnegative",
        ),
        CheckConstraint(
            "travel_after_buffer_minutes >= 0",
            name="travel_after_buffer_minutes_nonnegative",
        ),
        CheckConstraint(
            "human_control_window_minutes IN "
            "(5, 10, 20, 30, 60, 120, 240, 360, 720, 1440, 2160)",
            name="human_control_window_minutes_allowed",
        ),
        Index("ix_businesses_active", "active"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="America/Sao_Paulo",
        server_default="America/Sao_Paulo",
        nullable=False,
    )
    meta_phone_number_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    meta_waba_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slot_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=30, server_default="30", nullable=False
    )
    service_origin_address: Mapped[str] = mapped_column(
        String(500),
        default="Zona Leste de São José dos Campos - SP",
        server_default="Zona Leste de São José dos Campos - SP",
        nullable=False,
    )
    service_origin_latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6), nullable=True
    )
    service_origin_longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6), nullable=True
    )
    service_origin_is_precise: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    travel_calculation_method: Mapped[str] = mapped_column(
        String(32),
        default="configured_estimate",
        server_default="configured_estimate",
        nullable=False,
    )
    default_travel_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    travel_fallback_allowed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    travel_route_provider: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    travel_before_buffer_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    travel_after_buffer_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    travel_region_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    human_control_window_minutes: Mapped[int] = mapped_column(
        Integer, default=2160, server_default="2160", nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class BusinessAutomationExclusion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_automation_exclusions"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('ignore', 'human_only')",
            name="mode_allowed",
        ),
        CheckConstraint(
            "whatsapp_id ~ '^[1-9][0-9]{6,14}$'",
            name="whatsapp_id_normalized",
        ),
        UniqueConstraint(
            "business_id",
            "whatsapp_id",
            name="uq_business_automation_exclusions_business_whatsapp",
        ),
        Index(
            "ix_business_automation_exclusions_lookup",
            "business_id",
            "whatsapp_id",
            "active",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    whatsapp_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "whatsapp_id", name="uq_customers_business_whatsapp"
        ),
        UniqueConstraint(
            "business_id", "id", name="uq_customers_business_id_id"
        ),
        Index("ix_customers_business_id", "business_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    whatsapp_id: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "customer_id", name="uq_conversations_business_customer"
        ),
        UniqueConstraint(
            "business_id", "id", name="uq_conversations_business_id_id"
        ),
        ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_conversations_business_customer_customers",
        ),
        CheckConstraint(
            "suppression_reason IS NULL OR "
            "suppression_reason = 'manual_business_message'",
            name="suppression_reason_allowed",
        ),
        CheckConstraint(
            "conversation_initiated_by IS NULL OR "
            "conversation_initiated_by IN ('customer', 'business')",
            name="initiated_by_allowed",
        ),
        Index("ix_conversations_business_id", "business_id"),
        Index("ix_conversations_customer_id", "customer_id"),
        Index("ix_conversations_state", "state"),
        Index("ix_conversations_handoff_status", "handoff_status"),
        Index("ix_conversations_last_interaction_at", "last_interaction_at"),
        Index(
            "ix_conversations_automation_suppressed_until",
            "automation_suppressed_until",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    automation_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    handoff_status: Mapped[str] = mapped_column(
        String(32), default="none", server_default="none", nullable=False
    )
    last_interaction_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    automation_suppressed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suppression_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    human_control_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_human_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    conversation_initiated_by: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="duration_minutes_positive"),
        CheckConstraint(
            "pricing_type IN ('fixed', 'estimated', 'human_quote')",
            name="pricing_type_allowed",
        ),
        CheckConstraint(
            "included_quantity > 0", name="included_quantity_positive"
        ),
        CheckConstraint(
            "additional_unit_duration_minutes >= 0",
            name="additional_unit_duration_minutes_nonnegative",
        ),
        CheckConstraint(
            "additional_unit_price IS NULL OR additional_unit_price >= 0",
            name="additional_unit_price_nonnegative",
        ),
        CheckConstraint(
            "difficult_access_duration_minutes >= 0",
            name="difficult_access_duration_minutes_nonnegative",
        ),
        CheckConstraint(
            "difficult_access_price IS NULL OR difficult_access_price >= 0",
            name="difficult_access_price_nonnegative",
        ),
        CheckConstraint(
            "duration_margin_minutes >= 0",
            name="duration_margin_minutes_nonnegative",
        ),
        CheckConstraint(
            "unknown_access_policy IN ('standard', 'conservative', 'human_quote')",
            name="unknown_access_policy_allowed",
        ),
        UniqueConstraint("business_id", "id", name="uq_services_business_id_id"),
        Index("ix_services_business_id", "business_id"),
        Index("ix_services_active", "active"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    pricing_type: Mapped[str] = mapped_column(
        String(32), default="estimated", server_default="estimated", nullable=False
    )
    automatic_booking: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    included_quantity: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    additional_unit_duration_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    additional_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    requires_address: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    requires_quantity: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    considers_difficult_access: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    difficult_access_duration_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    difficult_access_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    unknown_access_policy: Mapped[str] = mapped_column(
        String(32),
        default="conservative",
        server_default="conservative",
        nullable=False,
    )
    duration_margin_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    asks_site_time_limit: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class Employee(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_employees_business_id_id"),
        Index("ix_employees_business_id", "business_id"),
        Index("ix_employees_active", "active"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class EmployeeService(Base):
    __tablename__ = "employee_services"
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_employee_services_business_employee_employees",
        ),
        ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_employee_services_business_service_services",
        ),
        Index("ix_employee_services_business_id", "business_id"),
        Index("ix_employee_services_service_id", "service_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )


class WorkingHours(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "working_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_time > start_time", name="end_time_after_start_time"),
        ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_working_hours_business_employee_employees",
        ),
        Index("ix_working_hours_business_id", "business_id"),
        Index("ix_working_hours_employee_weekday", "employee_id", "weekday"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)


class ScheduleBlock(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "schedule_blocks"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_at_after_starts_at"),
        ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_schedule_blocks_business_employee_employees",
        ),
        Index("ix_schedule_blocks_business_id", "business_id"),
        Index("ix_schedule_blocks_employee_starts_at", "employee_id", "starts_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_at_after_starts_at"),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'completed')",
            name="status_allowed",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "estimated_duration_minutes > 0",
            name="estimated_duration_minutes_positive",
        ),
        CheckConstraint(
            "travel_before_minutes >= 0",
            name="travel_before_minutes_nonnegative",
        ),
        CheckConstraint(
            "travel_after_minutes >= 0",
            name="travel_after_minutes_nonnegative",
        ),
        CheckConstraint(
            "pricing_type IN ('fixed', 'estimated', 'human_quote')",
            name="pricing_type_allowed",
        ),
        CheckConstraint(
            "access_condition IN ('normal', 'difficult', 'unknown')",
            name="access_condition_allowed",
        ),
        ForeignKeyConstraint(
            ["business_id", "customer_id"],
            ["customers.business_id", "customers.id"],
            name="fk_appointments_business_customer_customers",
        ),
        ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_appointments_business_service_services",
        ),
        ForeignKeyConstraint(
            ["business_id", "employee_id"],
            ["employees.business_id", "employees.id"],
            name="fk_appointments_business_employee_employees",
        ),
        ExcludeConstraint(
            ("employee_id", "="),
            (
                text(
                    "tstzrange("
                    "public.booking_add_minutes_immutable("
                    "starts_at, -travel_before_minutes), "
                    "public.booking_add_minutes_immutable("
                    "ends_at, travel_after_minutes), "
                    "'[)')"
                ),
                "&&",
            ),
            where=text("status = 'confirmed'"),
            using="gist",
            name="excl_appointments_employee_confirmed_overlap",
        ),
        Index("ix_appointments_business_id", "business_id"),
        Index("ix_appointments_customer_id", "customer_id"),
        Index("ix_appointments_service_id", "service_id"),
        Index("ix_appointments_employee_starts_at", "employee_id", "starts_at"),
        Index("ix_appointments_status", "status"),
        Index("ix_appointments_starts_at", "starts_at"),
        Index(
            "uq_appointments_idempotency_key_present",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    service_address: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    quantity: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    access_condition: Mapped[str] = mapped_column(
        String(16), default="normal", server_default="normal", nullable=False
    )
    estimated_duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    travel_before_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    travel_after_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    estimated_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    pricing_type: Mapped[str] = mapped_column(String(32), nullable=False)
    estimate_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    site_allowed_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="direction_allowed"
        ),
        ForeignKeyConstraint(
            ["business_id", "conversation_id"],
            ["conversations.business_id", "conversations.id"],
            name="fk_messages_business_conversation_conversations",
        ),
        Index("ix_messages_business_id", "business_id"),
        Index("ix_messages_conversation_id", "conversation_id"),
        Index("ix_messages_status", "status"),
        Index("ix_messages_created_at", "created_at"),
        Index(
            "uq_messages_provider_message_id_present",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index(
            "uq_messages_idempotency_key_present",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    interactive_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outbound_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ProcessedWebhook(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "processed_webhooks"
    __table_args__ = (
        Index("ix_processed_webhooks_provider_message_id", "provider_message_id"),
        Index("ix_processed_webhooks_status", "status"),
        Index("ix_processed_webhooks_received_at", "received_at"),
    )

    event_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

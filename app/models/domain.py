from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "whatsapp_id", name="uq_customers_business_whatsapp"
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
        Index("ix_conversations_business_id", "business_id"),
        Index("ix_conversations_customer_id", "customer_id"),
        Index("ix_conversations_state", "state"),
        Index("ix_conversations_handoff_status", "handoff_status"),
        Index("ix_conversations_last_interaction_at", "last_interaction_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False
    )
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


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="duration_minutes_positive"),
        Index("ix_services_business_id", "business_id"),
        Index("ix_services_active", "active"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )


class Employee(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
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
    __table_args__ = (Index("ix_employee_services_service_id", "service_id"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id"),
        primary_key=True,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("services.id"),
        primary_key=True,
    )


class WorkingHours(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "working_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_time > start_time", name="end_time_after_start_time"),
        Index("ix_working_hours_business_id", "business_id"),
        Index("ix_working_hours_employee_weekday", "employee_id", "weekday"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)


class ScheduleBlock(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "schedule_blocks"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_at_after_starts_at"),
        Index("ix_schedule_blocks_business_id", "business_id"),
        Index("ix_schedule_blocks_employee_starts_at", "employee_id", "starts_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False
    )
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
        ExcludeConstraint(
            ("employee_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
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
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="direction_allowed"
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

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    interactive_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.automation.domain import normalize_assistant_message

AppointmentStatus = Literal["pending", "confirmed", "cancelled", "completed"]
ConversationStatus = Literal["waiting", "in_progress", "answered"]
OperationalRole = Literal["technician", "assistant", "administrator"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppointmentView(StrictModel):
    id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    service_id: UUID
    service_name: str
    employee_id: UUID
    employee_name: str
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    notes: str | None
    reschedule_pending: bool = False
    rescheduled: bool = False
    reschedule_preferred_starts_at: datetime | None = None


class AppointmentCreate(StrictModel):
    customer_id: UUID
    service_id: UUID
    employee_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus = "pending"
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_interval(self) -> "AppointmentCreate":
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Appointment timestamps must include timezone")
        if self.ends_at <= self.starts_at:
            raise ValueError("Appointment end must be after start")
        return self


class AppointmentUpdate(StrictModel):
    customer_id: UUID | None = None
    service_id: UUID | None = None
    employee_id: UUID | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    status: AppointmentStatus | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_change(self) -> "AppointmentUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        for value in (self.starts_at, self.ends_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("Appointment timestamps must include timezone")
        return self


class AppointmentList(StrictModel):
    items: list[AppointmentView]


class AppointmentRescheduleRequest(StrictModel):
    preferred_starts_at: datetime | None = None
    force_conflicts: bool = False

    @field_validator("preferred_starts_at")
    @classmethod
    def validate_preferred_starts_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Preferred appointment timestamp must include timezone")
        return value


class AppointmentRescheduleConflict(StrictModel):
    appointment_id: UUID
    customer_name: str
    service_name: str
    starts_at: datetime


class AppointmentRescheduleResult(StrictModel):
    status: Literal["pending", "conflict"]
    appointment_id: UUID
    reason: Literal["appointment_conflict", "slot_unavailable"] | None = None
    displaced_appointment_ids: list[UUID] = Field(default_factory=list)
    conflicts: list[AppointmentRescheduleConflict] = Field(default_factory=list)
    message_ids: list[UUID] = Field(default_factory=list)


class DashboardMetrics(StrictModel):
    waiting_count: int
    in_progress_count: int
    appointments_today_count: int
    completed_today_count: int


class DashboardToday(StrictModel):
    metrics: DashboardMetrics
    upcoming_appointments: list[AppointmentView]


class NotificationView(StrictModel):
    id: UUID
    appointment_id: UUID
    event_type: Literal["automatic_booking_confirmed"]
    title: str
    body: str
    target_path: str
    read: bool
    created_at: datetime


class NotificationList(StrictModel):
    items: list[NotificationView]


class CustomerOutreachView(StrictModel):
    id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    outreach_type: Literal["incomplete_24h", "cleaning_6m"]
    status: Literal[
        "pending",
        "sent",
        "skipped",
        "responded",
        "accepted",
        "declined",
        "failed",
    ]
    service_label: str | None
    due_at: datetime
    sent_at: datetime | None
    responded_at: datetime | None
    source_appointment_id: UUID | None
    result_appointment_id: UUID | None
    result_appointment_path: str | None


class CustomerOutreachList(StrictModel):
    items: list[CustomerOutreachView]


class MessageView(StrictModel):
    id: UUID
    direction: Literal["inbound", "outbound"]
    message_type: str
    body: str | None
    status: str
    created_at: datetime
    media_mime_type: str | None = None
    media_filename: str | None = None
    media_url: str | None = None


class ConversationView(StrictModel):
    id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    customer_whatsapp_id: str | None = None
    last_content: str | None
    last_message_at: datetime | None
    status: ConversationStatus
    unread_count: int
    priority: bool
    pinned: bool = False
    manual_unread: bool = False
    assignee_name: str | None = None


class ConversationList(StrictModel):
    items: list[ConversationView]
    page: int
    page_size: int
    total: int


class ConversationDetail(ConversationView):
    messages: list[MessageView]
    assistant_enabled: bool
    automation_suppressed_until: datetime | None
    free_form_window_open: bool
    free_form_window_expires_at: datetime | None


class ConversationMessageDelta(StrictModel):
    items: list[MessageView]
    latest_at: datetime | None


class ConversationBulkAction(StrictModel):
    conversation_ids: list[UUID] = Field(min_length=1, max_length=100)
    action: Literal[
        "mark_read",
        "mark_unread",
        "pin",
        "unpin",
        "delete",
        "assistant_on",
        "assistant_off",
    ]

    @field_validator("conversation_ids")
    @classmethod
    def unique_conversations(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class ConversationBulkResult(StrictModel):
    affected: int


class CustomerNameUpdate(StrictModel):
    name: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            return None
        if len(normalized) > 255:
            raise ValueError("Customer name is too long")
        return normalized


class ManualMessageCreate(StrictModel):
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message cannot be empty")
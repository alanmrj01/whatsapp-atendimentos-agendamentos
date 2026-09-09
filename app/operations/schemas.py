from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AppointmentStatus = Literal["pending", "confirmed", "cancelled", "completed"]
ConversationStatus = Literal["waiting", "in_progress", "answered"]


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


class DashboardMetrics(StrictModel):
    waiting_count: int
    in_progress_count: int
    appointments_today_count: int
    completed_today_count: int


class DashboardToday(StrictModel):
    metrics: DashboardMetrics
    upcoming_appointments: list[AppointmentView]


class MessageView(StrictModel):
    id: UUID
    direction: Literal["inbound", "outbound"]
    message_type: str
    body: str | None
    status: str
    created_at: datetime


class ConversationView(StrictModel):
    id: UUID
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    last_content: str | None
    last_message_at: datetime | None
    status: ConversationStatus
    unread_count: int
    priority: bool
    assignee_name: str | None = None


class ConversationList(StrictModel):
    items: list[ConversationView]
    page: int
    page_size: int
    total: int


class ConversationDetail(ConversationView):
    messages: list[MessageView]


class BusinessView(StrictModel):
    id: UUID
    name: str
    timezone: str
    slot_interval_minutes: int


class BusinessUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    slot_interval_minutes: int | None = Field(default=None, ge=5, le=480)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Business name is required")
        return normalized

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Invalid timezone") from None
        return value

    @model_validator(mode="after")
    def require_change(self) -> "BusinessUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class WorkingHoursView(StrictModel):
    id: UUID
    employee_id: UUID
    employee_name: str
    weekday: int
    start_time: time
    end_time: time


class WorkingHoursCreate(StrictModel):
    employee_id: UUID
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_interval(self) -> "WorkingHoursCreate":
        if self.end_time <= self.start_time:
            raise ValueError("End time must be after start time")
        return self


class WorkingHoursUpdate(StrictModel):
    employee_id: UUID | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode="after")
    def require_change(self) -> "WorkingHoursUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class WorkingHoursList(StrictModel):
    items: list[WorkingHoursView]


class AutomationSettingsView(StrictModel):
    human_control_window_minutes: int
    supported_options: tuple[str, ...] = ("human_control_window_minutes",)


class AutomationSettingsUpdate(StrictModel):
    human_control_window_minutes: Literal[
        5, 10, 20, 30, 60, 120, 240, 360, 720, 1440, 2160
    ]


class EmployeeView(StrictModel):
    id: UUID
    name: str
    active: bool
    service_ids: list[UUID] = Field(default_factory=list)


class EmployeeCreate(StrictModel):
    name: str = Field(min_length=2, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Employee name is required")
        return normalized


class EmployeeUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Employee name is required")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "EmployeeUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class EmployeeList(StrictModel):
    items: list[EmployeeView]


class EmployeeServicesUpdate(StrictModel):
    service_ids: list[UUID]


class CustomerOption(StrictModel):
    id: UUID
    name: str
    phone: str | None


class CustomerList(StrictModel):
    items: list[CustomerOption]


class CustomerCreate(StrictModel):
    name: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=8, max_length=32, pattern=r"^\+[1-9][0-9]{7,14}$")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Customer name is required")
        return normalized


class ServiceOption(StrictModel):
    id: UUID
    name: str
    duration_minutes: int
    active: bool


class ServiceList(StrictModel):
    items: list[ServiceOption]


class ServiceCreate(StrictModel):
    name: str = Field(min_length=2, max_length=255)
    duration_minutes: int = Field(ge=1, le=1440)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Service name is required")
        return normalized


class ServiceUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Service name is required")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "ServiceUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class SetupStatus(StrictModel):
    company: bool
    business_hours: bool
    automation: bool
    agenda: bool
    whatsapp: bool
    completed: int
    total: Literal[5] = 5
    next_step: Literal[
        "company", "business_hours", "automation", "agenda", "whatsapp", "complete"
    ]

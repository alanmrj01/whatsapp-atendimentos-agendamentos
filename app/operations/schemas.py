from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Literal
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
        return normalized


class ConversationAutomationUpdate(StrictModel):
    enabled: bool


class ConversationActionUpdate(StrictModel):
    pinned: bool | None = None
    read: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ConversationActionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one conversation action is required")
        return self


class BusinessView(StrictModel):
    id: UUID
    name: str
    responsible_name: str | None
    timezone: str
    service_origin_address: str | None
    slot_interval_minutes: int
    interval_between_services_minutes: int | None
    preparation_minutes: int | None
    finishing_minutes: int | None
    minimum_booking_notice_minutes: int | None
    materials_catalog_reviewed: bool
    agenda_preferences_reviewed: bool
    onboarding_completed_at: datetime | None
    onboarding_version: int


class BusinessUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    responsible_name: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    service_origin_address: str | None = Field(default=None, max_length=500)
    slot_interval_minutes: int | None = Field(default=None, ge=5, le=480)
    interval_between_services_minutes: int | None = Field(default=None, ge=0, le=240)
    preparation_minutes: int | None = Field(default=None, ge=0, le=240)
    finishing_minutes: int | None = Field(default=None, ge=0, le=240)
    minimum_booking_notice_minutes: int | None = Field(default=None, ge=0, le=10080)
    materials_catalog_reviewed: bool | None = None
    agenda_preferences_reviewed: bool | None = None

    @field_validator("name", "responsible_name", "service_origin_address")
    @classmethod
    def normalize_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if info.field_name == "name" and len(normalized) < 2:
            raise ValueError("Business name is required")
        return normalized or None

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
    assistant_enabled: bool = True
    greeting_message: str = "Olá! Como posso ajudar com seu ar-condicionado?"
    fallback_message: str = (
        "Não entendi. Conte em poucas palavras o serviço que você precisa."
    )
    handoff_message: str = (
        "Seu atendimento foi encaminhado para uma pessoa da equipe."
    )
    supported_options: tuple[str, ...] = (
        "assistant_enabled",
        "human_control_window_minutes",
        "greeting_message",
        "fallback_message",
        "handoff_message",
    )


class AutomationSettingsUpdate(StrictModel):
    human_control_window_minutes: Literal[
        5, 10, 20, 30, 60, 120, 240, 360, 720, 1440, 2160
    ] | None = None
    assistant_enabled: bool | None = None
    greeting_message: str | None = None
    fallback_message: str | None = None
    handoff_message: str | None = None

    @field_validator("greeting_message", "fallback_message", "handoff_message")
    @classmethod
    def validate_message(cls, value: str | None) -> str | None:
        return normalize_assistant_message(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "AutomationSettingsUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class AutomationExclusionView(StrictModel):
    id: UUID
    whatsapp_id: str
    mode: Literal["ignore", "human_only"]
    label: str | None
    reason: str | None
    active: bool


class AutomationExclusionList(StrictModel):
    items: list[AutomationExclusionView]


class EmployeeView(StrictModel):
    id: UUID
    name: str
    active: bool
    operational_role: OperationalRole
    service_ids: list[UUID] = Field(default_factory=list)


class EmployeeCreate(StrictModel):
    name: str = Field(min_length=2, max_length=255)
    operational_role: OperationalRole = "technician"

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
    operational_role: OperationalRole | None = None

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
        if (
            "operational_role" in self.model_fields_set
            and self.operational_role is None
        ):
            raise ValueError("operational_role cannot be null")
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
    price: Decimal | None
    active: bool
    intent_examples: list[str] = Field(default_factory=list)


class ServiceList(StrictModel):
    items: list[ServiceOption]


class ServiceCreate(StrictModel):
    name: str = Field(min_length=2, max_length=255)
    duration_minutes: int = Field(ge=1, le=1440)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)

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
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    active: bool | None = None
    intent_examples: list[str] | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("Service name is required")
        return normalized

    @field_validator("intent_examples")
    @classmethod
    def normalize_examples(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for item in value:
            sentence = " ".join(item.split())
            if sentence and sentence not in normalized:
                normalized.append(sentence[:300])
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "ServiceUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class CatalogItemView(StrictModel):
    id: UUID
    kind: Literal["material", "equipment"]
    name: str
    description: str | None
    price: Decimal | None
    unit_label: str | None
    preset_key: str | None
    active: bool


class CatalogItemCreate(StrictModel):
    kind: Literal["material", "equipment"] = "material"
    name: str = Field(min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    unit_label: str | None = Field(default=None, max_length=64)


class CatalogItemUpdate(StrictModel):
    kind: Literal["material", "equipment"] | None = None
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    unit_label: str | None = Field(default=None, max_length=64)
    active: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "CatalogItemUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        return self


class CatalogItemList(StrictModel):
    items: list[CatalogItemView]


class SetupStatus(StrictModel):
    company: bool
    team: bool
    business_hours: bool
    services: bool
    materials: bool
    agenda: bool
    whatsapp: bool
    completed: int
    total: Literal[7] = 7
    next_step: Literal[
        "company", "team", "business_hours", "services", "materials", "agenda", "whatsapp", "complete"
    ]
    onboarding_completed: bool
    onboarding_completed_at: datetime | None
    onboarding_version: int
    blocking_reasons: list[str] = Field(default_factory=list)

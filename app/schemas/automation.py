from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.automation.domain import (
    DEFAULT_ASSISTANT_FALLBACK,
    DEFAULT_ASSISTANT_GREETING,
    DEFAULT_ASSISTANT_HANDOFF,
    DEFAULT_HUMAN_CONTROL_WINDOW_MINUTES,
    ExclusionMode,
    normalize_whatsapp_id,
    normalize_assistant_message,
    validate_human_control_window,
)


class AutomationExclusionCreate(BaseModel):
    whatsapp_id: str
    mode: ExclusionMode
    label: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    active: bool = True

    model_config = ConfigDict(extra="forbid")

    @field_validator("whatsapp_id")
    @classmethod
    def validate_whatsapp_id(cls, value: str) -> str:
        return normalize_whatsapp_id(value)


class AutomationExclusionUpdate(BaseModel):
    mode: ExclusionMode | None = None
    label: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)
    active: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> "AutomationExclusionUpdate":
        for field_name in ("mode", "active"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class BusinessAutomationSettings(BaseModel):
    business_id: uuid.UUID
    human_control_window_minutes: int = DEFAULT_HUMAN_CONTROL_WINDOW_MINUTES
    assistant_enabled: bool = True
    greeting_message: str = DEFAULT_ASSISTANT_GREETING
    fallback_message: str = DEFAULT_ASSISTANT_FALLBACK
    handoff_message: str = DEFAULT_ASSISTANT_HANDOFF

    model_config = ConfigDict(extra="forbid")

    @field_validator("human_control_window_minutes")
    @classmethod
    def validate_window(cls, value: int) -> int:
        return validate_human_control_window(value)

    @field_validator("greeting_message", "fallback_message", "handoff_message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return normalize_assistant_message(value)


class BusinessAutomationSettingsUpdate(BaseModel):
    human_control_window_minutes: int | None = None
    assistant_enabled: bool | None = None
    greeting_message: str | None = None
    fallback_message: str | None = None
    handoff_message: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("human_control_window_minutes")
    @classmethod
    def validate_window(cls, value: int | None) -> int | None:
        return validate_human_control_window(value) if value is not None else None

    @field_validator("greeting_message", "fallback_message", "handoff_message")
    @classmethod
    def validate_message(cls, value: str | None) -> str | None:
        return normalize_assistant_message(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "BusinessAutomationSettingsUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self

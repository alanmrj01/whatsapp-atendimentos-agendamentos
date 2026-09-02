from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.automation.domain import (
    DEFAULT_HUMAN_CONTROL_WINDOW_MINUTES,
    ExclusionMode,
    normalize_whatsapp_id,
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

    model_config = ConfigDict(extra="forbid")

    @field_validator("human_control_window_minutes")
    @classmethod
    def validate_window(cls, value: int) -> int:
        return validate_human_control_window(value)


class BusinessAutomationSettingsUpdate(BaseModel):
    human_control_window_minutes: int

    model_config = ConfigDict(extra="forbid")

    @field_validator("human_control_window_minutes")
    @classmethod
    def validate_window(cls, value: int) -> int:
        return validate_human_control_window(value)

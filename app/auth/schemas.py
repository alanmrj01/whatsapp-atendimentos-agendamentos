from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.auth.security import normalize_email
from app.whatsapp.onboarding import WhatsAppOnboardingIntent


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ATTENDANT = "attendant"
    VIEWER = "viewer"


AccessMode = Literal["free", "paid"]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LoginRequest(StrictRequest):
    email: str = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return normalize_email(value)


class SignupRequest(StrictRequest):
    business_name: str = Field(min_length=2, max_length=255)
    email: str = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=1024)

    @field_validator("business_name")
    @classmethod
    def normalized_business_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if len(value) < 2:
            raise ValueError("Business name is required")
        return value

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return normalize_email(value)


class EmptyRequest(StrictRequest):
    pass


class ActiveBusinessRequest(StrictRequest):
    business_id: UUID


class PublicPlanRequest(StrictRequest):
    intent: WhatsAppOnboardingIntent
    platform_only_impact_confirmed: bool = Field(default=False, strict=True)


class AccessResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 600


class MembershipResponse(BaseModel):
    business_id: UUID
    business_name: str
    role: MembershipRole
    access_mode: AccessMode = "free"


class MeResponse(BaseModel):
    id: UUID
    email: str
    platform_role: Literal["super_admin"] | None
    active_business_id: UUID | None
    memberships: list[MembershipResponse]


class PublicConnectionResponse(BaseModel):
    status: Literal["disconnected", "pending", "connected", "error"]
    mode: Literal["coexistence", "api_only"] | None = None

from __future__ import annotations

from datetime import datetime
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


class PasswordChangeRequest(StrictRequest):
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class PasswordResetRequest(StrictRequest):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return normalize_email(value)


class PasswordResetConfirmRequest(StrictRequest):
    token: SecretStr = Field(min_length=32, max_length=256)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class EmptyRequest(StrictRequest):
    pass


class ActiveBusinessRequest(StrictRequest):
    business_id: UUID


class PublicPlanRequest(StrictRequest):
    intent: WhatsAppOnboardingIntent
    platform_only_impact_confirmed: bool = Field(default=False, strict=True)


class MembershipResponse(BaseModel):
    business_id: UUID
    business_name: str
    role: MembershipRole
    access_mode: AccessMode = "free"
    has_had_operational_access: bool = False
    admin_full_access: bool = False
    account_state: Literal["demo", "active", "payment_blocked"] = "demo"


class MeResponse(BaseModel):
    id: UUID
    email: str
    platform_role: Literal["super_admin"] | None
    active_business_id: UUID | None
    memberships: list[MembershipResponse]


class AccessResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 600
    # Additive session snapshot lets the PWA hydrate after login/refresh without
    # a second network round-trip. Older clients may safely ignore this field.
    session: MeResponse


class PublicConnectionResponse(BaseModel):
    status: Literal["disconnected", "pending", "connected", "error"]
    mode: Literal["coexistence", "api_only"] | None = None
    display_phone_number: str | None = None
    pending_state: Literal["authorization_pending"] | None = None
    review_status: Literal["approved", "rejected"] | None = None
    preferred_mode: Literal["coexistence", "api_only"] | None = None
    mode_switch_requested_at: datetime | None = None
    mode_switch_last_checked_at: datetime | None = None
    mode_switch_next_check_at: datetime | None = None


class WhatsAppModePreferenceRequest(StrictRequest):
    preferred_mode: Literal["coexistence", "api_only"] | None = None


class MetaEmbeddedSignupStartResponse(BaseModel):
    app_id: str
    configuration_id: str
    graph_version: str
    embedded_signup_version: str
    mode: Literal["coexistence"] = "coexistence"


MetaEmbeddedSignupTelemetryStage = Literal[
    "sdk_ready",
    "login_opened",
    "login_callback_received",
    "wa_session_event_received",
    "intermediate_step_received",
    "page_hidden",
    "page_visible",
    "window_focus",
    "pageshow",
    "popup_closed",
    "embedded_signup_ready_to_complete",
    "complete_request_started",
    "complete_request_succeeded",
    "complete_request_failed",
    "timeout",
]


class MetaEmbeddedSignupTelemetryRequest(StrictRequest):
    stage: MetaEmbeddedSignupTelemetryStage
    authorization_code_received: bool | None = Field(default=None, strict=True)
    waba_id_received: bool | None = Field(default=None, strict=True)
    phone_number_id_received: bool | None = Field(default=None, strict=True)
    intermediate_step_received: bool | None = Field(default=None, strict=True)


class MetaEmbeddedSignupAssetsRequest(StrictRequest):
    waba_id: str = Field(min_length=1, max_length=32)
    phone_number_id: str | None = Field(default=None, min_length=1, max_length=32)


class MetaEmbeddedSignupCompleteRequest(StrictRequest):
    authorization_code: SecretStr = Field(min_length=1, max_length=4096)
    waba_id: str = Field(min_length=1, max_length=32)
    phone_number_id: str | None = Field(default=None, min_length=1, max_length=32)


class MetaApiOnlyEmbeddedSignupStartRequest(StrictRequest):
    intent: WhatsAppOnboardingIntent
    platform_only_impact_confirmed: bool = Field(default=False, strict=True)

    @field_validator("intent")
    @classmethod
    def api_only_intent(cls, value: WhatsAppOnboardingIntent) -> WhatsAppOnboardingIntent:
        if value not in {
            WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER,
            WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY,
        }:
            raise ValueError("API-only onboarding intent is invalid")
        return value


class MetaApiOnlyEmbeddedSignupStartResponse(BaseModel):
    app_id: str
    configuration_id: str
    graph_version: str
    embedded_signup_version: str
    mode: Literal["api_only"] = "api_only"
    intent: WhatsAppOnboardingIntent


class MetaApiOnlyEmbeddedSignupCompleteRequest(MetaApiOnlyEmbeddedSignupStartRequest):
    authorization_code: SecretStr = Field(min_length=1, max_length=4096)
    waba_id: str = Field(min_length=1, max_length=32)
    phone_number_id: str | None = Field(default=None, min_length=1, max_length=32)
    registration_pin: SecretStr = Field(min_length=6, max_length=6)

    @field_validator("registration_pin")
    @classmethod
    def six_digit_registration_pin(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) != 6 or not raw.isdigit():
            raise ValueError("Registration PIN must contain exactly 6 digits")
        return value

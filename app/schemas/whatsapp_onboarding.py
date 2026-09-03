from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.whatsapp.administration import WhatsAppConnectionStatusView
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
)
from app.whatsapp.onboarding import (
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingNextStep,
    WhatsAppOnboardingPlan,
)


class WhatsAppOnboardingPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: WhatsAppOnboardingIntent
    platform_only_impact_confirmed: bool = False


class WhatsAppOnboardingPlanResponse(BaseModel):
    intent: WhatsAppOnboardingIntent
    requested_mode: WhatsAppConnectionMode
    next_step: WhatsAppOnboardingNextStep
    ready_to_continue: bool
    requires_meta_eligibility_check: bool
    requires_explicit_platform_only_confirmation: bool
    client_message_code: str


class WhatsAppConnectionResponse(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    provider: WhatsAppProvider
    mode: WhatsAppConnectionMode
    status: WhatsAppConnectionStatus
    has_phone_number_id: bool
    has_credential_reference: bool
    connected_at: datetime | None
    disconnected_at: datetime | None
    last_error_code: str | None


class WhatsAppCurrentConnectionResponse(BaseModel):
    connected: bool
    connection: WhatsAppConnectionResponse | None


class WhatsAppOnboardingCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: WhatsAppOnboardingIntent
    confirmed_mode: WhatsAppConnectionMode
    provider_confirmed: Literal[True]
    platform_only_impact_confirmed: bool = False
    meta_waba_id: str = Field(min_length=1, max_length=255)
    meta_phone_number_id: str = Field(min_length=1, max_length=255)
    graph_version: str = Field(min_length=1, max_length=32)
    credential_secret_ref: str = Field(min_length=1, max_length=512)


def onboarding_plan_response(
    plan: WhatsAppOnboardingPlan,
) -> WhatsAppOnboardingPlanResponse:
    return WhatsAppOnboardingPlanResponse(
        intent=plan.intent,
        requested_mode=plan.requested_mode,
        next_step=plan.next_step,
        ready_to_continue=plan.ready_to_continue,
        requires_meta_eligibility_check=plan.requires_meta_eligibility_check,
        requires_explicit_platform_only_confirmation=(
            plan.requires_explicit_platform_only_confirmation
        ),
        client_message_code=plan.client_message_code,
    )


def connection_response(
    view: WhatsAppConnectionStatusView,
) -> WhatsAppConnectionResponse:
    return WhatsAppConnectionResponse(
        id=view.id,
        business_id=view.business_id,
        provider=view.provider,
        mode=view.mode,
        status=view.status,
        has_phone_number_id=view.has_phone_number_id,
        has_credential_reference=view.has_credential_reference,
        connected_at=view.connected_at,
        disconnected_at=view.disconnected_at,
        last_error_code=view.last_error_code,
    )

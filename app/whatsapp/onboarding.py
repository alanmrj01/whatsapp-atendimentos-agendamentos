from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.whatsapp.administration import WhatsAppConnectionStatusView
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionStatus,
    validate_credential_secret_ref,
    validate_graph_version,
    validate_meta_identifier,
)


class WhatsAppOnboardingError(RuntimeError):
    """Falha sanitizada no contrato de onboarding do WhatsApp."""


class WhatsAppOnboardingIntent(str, Enum):
    KEEP_WHATSAPP_BUSINESS = "keep_whatsapp_business"
    USE_NEW_OR_DEDICATED_NUMBER = "use_new_or_dedicated_number"
    USE_EXISTING_NUMBER_PLATFORM_ONLY = "use_existing_number_platform_only"


class WhatsAppOnboardingNextStep(str, Enum):
    META_EMBEDDED_SIGNUP = "meta_embedded_signup"
    CONFIRM_PLATFORM_ONLY_IMPACT = "confirm_platform_only_impact"


@dataclass(frozen=True, slots=True)
class WhatsAppOnboardingPlan:
    intent: WhatsAppOnboardingIntent
    requested_mode: WhatsAppConnectionMode
    next_step: WhatsAppOnboardingNextStep
    ready_to_continue: bool
    requires_meta_eligibility_check: bool
    requires_explicit_platform_only_confirmation: bool
    client_message_code: str


@dataclass(frozen=True, slots=True)
class WhatsAppProviderCompletion:
    intent: WhatsAppOnboardingIntent
    confirmed_mode: WhatsAppConnectionMode
    meta_waba_id: str
    meta_phone_number_id: str
    graph_version: str
    credential_secret_ref: str
    provider_confirmed: bool
    platform_only_impact_confirmed: bool = False


class WhatsAppOnboardingAdministrationPort(Protocol):
    async def create_pending_connection(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView: ...

    async def get_connection(
        self,
        business_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WhatsAppConnectionStatusView | None: ...

    async def update_meta_identifiers(
        self,
        business_id: uuid.UUID,
        *,
        meta_waba_id: str | None,
        meta_phone_number_id: str | None,
        display_phone_number: str | None = None,
        graph_version: str | None = None,
    ) -> WhatsAppConnectionStatusView: ...

    async def set_credential_secret_ref(
        self,
        business_id: uuid.UUID,
        credential_secret_ref: str,
    ) -> WhatsAppConnectionStatusView: ...

    async def change_mode(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView: ...

    async def mark_connected(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView: ...

    async def mark_disconnected(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView: ...


def build_onboarding_plan(
    intent: WhatsAppOnboardingIntent,
    *,
    platform_only_impact_confirmed: bool = False,
) -> WhatsAppOnboardingPlan:
    normalized_intent = WhatsAppOnboardingIntent(intent)

    if normalized_intent is WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS:
        return WhatsAppOnboardingPlan(
            intent=normalized_intent,
            requested_mode=WhatsAppConnectionMode.COEXISTENCE,
            next_step=WhatsAppOnboardingNextStep.META_EMBEDDED_SIGNUP,
            ready_to_continue=True,
            requires_meta_eligibility_check=True,
            requires_explicit_platform_only_confirmation=False,
            client_message_code="COEXISTENCE_PREFERRED",
        )

    if normalized_intent is WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER:
        return WhatsAppOnboardingPlan(
            intent=normalized_intent,
            requested_mode=WhatsAppConnectionMode.API_ONLY,
            next_step=WhatsAppOnboardingNextStep.META_EMBEDDED_SIGNUP,
            ready_to_continue=True,
            requires_meta_eligibility_check=False,
            requires_explicit_platform_only_confirmation=False,
            client_message_code="PLATFORM_NUMBER",
        )

    if normalized_intent is WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY:
        if not platform_only_impact_confirmed:
            return WhatsAppOnboardingPlan(
                intent=normalized_intent,
                requested_mode=WhatsAppConnectionMode.API_ONLY,
                next_step=WhatsAppOnboardingNextStep.CONFIRM_PLATFORM_ONLY_IMPACT,
                ready_to_continue=False,
                requires_meta_eligibility_check=False,
                requires_explicit_platform_only_confirmation=True,
                client_message_code="CONFIRM_PLATFORM_ONLY_IMPACT",
            )
        return WhatsAppOnboardingPlan(
            intent=normalized_intent,
            requested_mode=WhatsAppConnectionMode.API_ONLY,
            next_step=WhatsAppOnboardingNextStep.META_EMBEDDED_SIGNUP,
            ready_to_continue=True,
            requires_meta_eligibility_check=False,
            requires_explicit_platform_only_confirmation=True,
            client_message_code="PLATFORM_ONLY_CONFIRMED",
        )

    raise WhatsAppOnboardingError("WhatsApp onboarding intent is invalid")


class WhatsAppOnboardingService:
    def __init__(self, administration: WhatsAppOnboardingAdministrationPort) -> None:
        self._administration = administration

    def plan(
        self,
        intent: WhatsAppOnboardingIntent,
        *,
        platform_only_impact_confirmed: bool = False,
    ) -> WhatsAppOnboardingPlan:
        try:
            return build_onboarding_plan(
                intent,
                platform_only_impact_confirmed=platform_only_impact_confirmed,
            )
        except (TypeError, ValueError):
            raise WhatsAppOnboardingError(
                "WhatsApp onboarding intent is invalid"
            ) from None

    async def complete_provider_onboarding(
        self,
        business_id: uuid.UUID,
        completion: WhatsAppProviderCompletion,
    ) -> WhatsAppConnectionStatusView:
        if not completion.provider_confirmed:
            raise WhatsAppOnboardingError(
                "WhatsApp provider confirmation is required"
            )

        plan = self.plan(
            completion.intent,
            platform_only_impact_confirmed=(
                completion.platform_only_impact_confirmed
            ),
        )
        if not plan.ready_to_continue:
            raise WhatsAppOnboardingError(
                "Explicit platform-only confirmation is required"
            )
        if completion.confirmed_mode is not plan.requested_mode:
            raise WhatsAppOnboardingError(
                "Provider mode does not match the approved onboarding path"
            )

        try:
            meta_waba_id = validate_meta_identifier(completion.meta_waba_id)
            meta_phone_number_id = validate_meta_identifier(
                completion.meta_phone_number_id
            )
            graph_version = validate_graph_version(completion.graph_version)
            credential_secret_ref = validate_credential_secret_ref(
                completion.credential_secret_ref
            )
        except ValueError:
            raise WhatsAppOnboardingError(
                "WhatsApp provider completion is invalid"
            ) from None
        if any(
            value is None
            for value in (meta_waba_id, meta_phone_number_id, graph_version)
        ):
            raise WhatsAppOnboardingError(
                "WhatsApp provider completion is incomplete"
            )

        # Serialize from the first state decision. Without this lock, a second
        # completion could wait on a later update and overwrite a row that had
        # become connected while it was waiting.
        current = await self._administration.get_connection(
            business_id, for_update=True
        )
        if current is None or current.status is WhatsAppConnectionStatus.DISCONNECTED:
            await self._administration.create_pending_connection(
                business_id,
                completion.confirmed_mode,
            )
        elif current.status is WhatsAppConnectionStatus.CONNECTED:
            raise WhatsAppOnboardingError(
                "Business already has a connected WhatsApp account"
            )
        elif current.mode is not completion.confirmed_mode:
            await self._administration.change_mode(
                business_id,
                completion.confirmed_mode,
            )

        await self._administration.update_meta_identifiers(
            business_id,
            meta_waba_id=meta_waba_id,
            meta_phone_number_id=meta_phone_number_id,
            graph_version=graph_version,
        )
        await self._administration.set_credential_secret_ref(
            business_id,
            credential_secret_ref,
        )
        return await self._administration.mark_connected(business_id)

    async def disconnect(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView:
        current = await self._administration.get_connection(
            business_id, for_update=True
        )
        if current is None:
            raise WhatsAppOnboardingError(
                "WhatsApp business connection is not available"
            )
        if current.status is WhatsAppConnectionStatus.DISCONNECTED:
            return current
        return await self._administration.mark_disconnected(business_id)

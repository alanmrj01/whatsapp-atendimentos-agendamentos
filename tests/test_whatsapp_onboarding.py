from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.schemas.whatsapp_onboarding import WhatsAppOnboardingCompleteRequest
from app.whatsapp.administration import WhatsAppConnectionStatusView
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
)
from app.whatsapp.onboarding import (
    WhatsAppOnboardingError,
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingNextStep,
    WhatsAppOnboardingService,
    WhatsAppProviderCompletion,
    build_onboarding_plan,
)

BUSINESS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def view(
    *,
    mode: WhatsAppConnectionMode,
    status: WhatsAppConnectionStatus,
) -> WhatsAppConnectionStatusView:
    return WhatsAppConnectionStatusView(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        business_id=BUSINESS_ID,
        provider=WhatsAppProvider.META,
        mode=mode,
        status=status,
        has_phone_number_id=status is WhatsAppConnectionStatus.CONNECTED,
        has_credential_reference=status is WhatsAppConnectionStatus.CONNECTED,
        connected_at=(
            datetime.now(timezone.utc)
            if status is WhatsAppConnectionStatus.CONNECTED
            else None
        ),
        disconnected_at=(
            datetime.now(timezone.utc)
            if status is WhatsAppConnectionStatus.DISCONNECTED
            else None
        ),
        last_error_code=None,
    )


class FakeAdministration:
    def __init__(
        self,
        current: WhatsAppConnectionStatusView | None = None,
    ) -> None:
        self.current = current
        self.calls: list[tuple[str, object]] = []

    async def create_pending_connection(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("create_pending_connection", mode))
        self.current = view(mode=mode, status=WhatsAppConnectionStatus.PENDING)
        return self.current

    async def get_connection(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView | None:
        self.calls.append(("get_connection", business_id))
        return self.current

    async def update_meta_identifiers(
        self,
        business_id: uuid.UUID,
        *,
        meta_waba_id: str | None,
        meta_phone_number_id: str | None,
        display_phone_number: str | None = None,
        graph_version: str | None = None,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("update_meta_identifiers", meta_phone_number_id))
        assert self.current is not None
        return self.current

    async def set_credential_secret_ref(
        self,
        business_id: uuid.UUID,
        credential_secret_ref: str,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("set_credential_secret_ref", "configured"))
        assert self.current is not None
        return self.current

    async def change_mode(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("change_mode", mode))
        assert self.current is not None
        self.current = view(mode=mode, status=self.current.status)
        return self.current

    async def mark_connected(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("mark_connected", business_id))
        assert self.current is not None
        self.current = view(
            mode=self.current.mode,
            status=WhatsAppConnectionStatus.CONNECTED,
        )
        return self.current

    async def mark_disconnected(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppConnectionStatusView:
        self.calls.append(("mark_disconnected", business_id))
        assert self.current is not None
        self.current = view(
            mode=self.current.mode,
            status=WhatsAppConnectionStatus.DISCONNECTED,
        )
        return self.current


def completion(
    *,
    intent: WhatsAppOnboardingIntent,
    mode: WhatsAppConnectionMode,
    impact_confirmed: bool = False,
) -> WhatsAppProviderCompletion:
    return WhatsAppProviderCompletion(
        intent=intent,
        confirmed_mode=mode,
        meta_waba_id="waba-id",
        meta_phone_number_id="phone-id",
        graph_version="v23.0",
        credential_secret_ref=(
            "projects/project-a/secrets/whatsapp-token/versions/latest"
        ),
        provider_confirmed=True,
        platform_only_impact_confirmed=impact_confirmed,
    )


def test_existing_business_app_prefers_coexistence() -> None:
    plan = build_onboarding_plan(
        WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS
    )

    assert plan.requested_mode is WhatsAppConnectionMode.COEXISTENCE
    assert plan.ready_to_continue is True
    assert plan.requires_meta_eligibility_check is True
    assert plan.next_step is WhatsAppOnboardingNextStep.META_EMBEDDED_SIGNUP


def test_new_number_uses_api_only_without_coexistence_check() -> None:
    plan = build_onboarding_plan(
        WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER
    )

    assert plan.requested_mode is WhatsAppConnectionMode.API_ONLY
    assert plan.ready_to_continue is True
    assert plan.requires_meta_eligibility_check is False


def test_existing_number_platform_only_requires_explicit_confirmation() -> None:
    initial = build_onboarding_plan(
        WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY
    )
    confirmed = build_onboarding_plan(
        WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY,
        platform_only_impact_confirmed=True,
    )

    assert initial.ready_to_continue is False
    assert initial.next_step is (
        WhatsAppOnboardingNextStep.CONFIRM_PLATFORM_ONLY_IMPACT
    )
    assert confirmed.ready_to_continue is True
    assert confirmed.requested_mode is WhatsAppConnectionMode.API_ONLY


@pytest.mark.asyncio
async def test_completion_creates_only_provider_confirmed_mode() -> None:
    administration = FakeAdministration()
    service = WhatsAppOnboardingService(administration)

    result = await service.complete_provider_onboarding(
        BUSINESS_ID,
        completion(
            intent=WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
            mode=WhatsAppConnectionMode.COEXISTENCE,
        ),
    )

    assert result.status is WhatsAppConnectionStatus.CONNECTED
    assert result.mode is WhatsAppConnectionMode.COEXISTENCE
    assert (
        "create_pending_connection",
        WhatsAppConnectionMode.COEXISTENCE,
    ) in administration.calls
    assert any(
        name == "set_credential_secret_ref"
        for name, _ in administration.calls
    )


@pytest.mark.asyncio
async def test_completion_never_silently_falls_back_to_api_only() -> None:
    service = WhatsAppOnboardingService(FakeAdministration())

    with pytest.raises(WhatsAppOnboardingError):
        await service.complete_provider_onboarding(
            BUSINESS_ID,
            completion(
                intent=WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
                mode=WhatsAppConnectionMode.API_ONLY,
            ),
        )


@pytest.mark.asyncio
async def test_existing_number_api_only_requires_impact_confirmation() -> None:
    service = WhatsAppOnboardingService(FakeAdministration())

    with pytest.raises(WhatsAppOnboardingError):
        await service.complete_provider_onboarding(
            BUSINESS_ID,
            completion(
                intent=(
                    WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY
                ),
                mode=WhatsAppConnectionMode.API_ONLY,
                impact_confirmed=False,
            ),
        )


@pytest.mark.asyncio
async def test_connected_account_cannot_be_recompleted() -> None:
    service = WhatsAppOnboardingService(
        FakeAdministration(
            view(
                mode=WhatsAppConnectionMode.COEXISTENCE,
                status=WhatsAppConnectionStatus.CONNECTED,
            )
        )
    )

    with pytest.raises(WhatsAppOnboardingError):
        await service.complete_provider_onboarding(
            BUSINESS_ID,
            completion(
                intent=WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
                mode=WhatsAppConnectionMode.COEXISTENCE,
            ),
        )


@pytest.mark.asyncio
async def test_disconnect_is_idempotent_for_disconnected_connection() -> None:
    disconnected = view(
        mode=WhatsAppConnectionMode.API_ONLY,
        status=WhatsAppConnectionStatus.DISCONNECTED,
    )
    administration = FakeAdministration(disconnected)
    service = WhatsAppOnboardingService(administration)

    result = await service.disconnect(BUSINESS_ID)

    assert result is disconnected
    assert not any(
        name == "mark_disconnected" for name, _ in administration.calls
    )


def test_completion_contract_never_accepts_raw_access_token() -> None:
    fields = WhatsAppOnboardingCompleteRequest.model_fields

    assert "access_token" not in fields
    assert "meta_access_token" not in fields
    assert "credential_secret_ref" in fields

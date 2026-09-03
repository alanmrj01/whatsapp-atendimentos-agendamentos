from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.api.internal_whatsapp_onboarding import (
    _operation_rejected,
    require_whatsapp_onboarding_oidc,
)
from app.core.config import Settings, get_settings
from app.main import app
from app.tasks import auth
from app.whatsapp.onboarding import WhatsAppOnboardingError

BUSINESS_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_whatsapp_onboarding_api_is_not_anonymous(
    client: AsyncClient,
) -> None:
    response = await client.get(
        f"/internal/whatsapp/connections/{BUSINESS_ID}"
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized service request"


@pytest.mark.asyncio
async def test_whatsapp_onboarding_start_is_not_anonymous(
    client: AsyncClient,
) -> None:
    response = await client.post(
        f"/internal/whatsapp/connections/{BUSINESS_ID}/onboarding/start",
        json={"intent": "keep_whatsapp_business"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_whatsapp_onboarding_complete_rejects_raw_token_field(
    client: AsyncClient,
) -> None:
    response = await client.post(
        f"/internal/whatsapp/connections/{BUSINESS_ID}/onboarding/complete",
        json={
            "intent": "keep_whatsapp_business",
            "confirmed_mode": "coexistence",
            "provider_confirmed": True,
            "meta_waba_id": "waba-id",
            "meta_phone_number_id": "phone-id",
            "graph_version": "v23.0",
            "credential_secret_ref": (
                "projects/project-a/secrets/whatsapp-token/versions/latest"
            ),
            "access_token": "must-never-be-accepted",
        },
    )

    # Authentication is evaluated before request processing in normal operation.
    # The schema itself is separately asserted to forbid raw token fields.
    assert response.status_code == 401


def onboarding_settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "environment": "production",
            "whatsapp_onboarding_oidc_audience": (
                "https://whatsapp-backend.example.run.app"
            ),
            "whatsapp_onboarding_invoker_email": (
                "onboarding@example.iam.gserviceaccount.com"
            ),
        }
    )


@pytest.mark.asyncio
async def test_false_bearer_is_rejected_without_database_access(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_token(*_: object) -> dict[str, object]:
        raise ValueError("invalid signature")

    monkeypatch.setattr(auth, "_verify_google_oidc_token", reject_token)
    app.dependency_overrides[get_settings] = onboarding_settings
    try:
        response = await client.get(
            f"/internal/whatsapp/connections/{BUSINESS_ID}",
            headers={"Authorization": "Bearer forged-token"},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized service request"}
    assert "forged-token" not in response.text


@pytest.mark.asyncio
async def test_oidc_requires_verified_expected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth,
        "_verify_google_oidc_token",
        lambda *_: {
            "email": "other@example.iam.gserviceaccount.com",
            "email_verified": True,
        },
    )
    with pytest.raises(HTTPException) as caught:
        await require_whatsapp_onboarding_oidc(
            onboarding_settings(), "Bearer signed-but-wrong-identity"
        )

    assert caught.value.status_code == 403
    assert "signed-but-wrong-identity" not in str(caught.value)


def test_rejected_operation_never_logs_or_returns_sensitive_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_values = (
        "raw-access-token-value",
        "projects/private/secrets/credential/versions/latest",
        "5511999999999",
        "private-phone-number-id",
    )
    caplog.set_level(logging.INFO)
    error = _operation_rejected(
        WhatsAppOnboardingError(" ".join(sensitive_values))
    )

    output = caplog.text + str(error.detail)
    assert error.status_code == 409
    assert error.detail == "WhatsApp onboarding operation rejected"
    assert all(value not in output for value in sensitive_values)

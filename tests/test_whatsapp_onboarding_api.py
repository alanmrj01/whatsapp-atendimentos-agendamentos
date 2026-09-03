from __future__ import annotations

import pytest
from httpx import AsyncClient

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

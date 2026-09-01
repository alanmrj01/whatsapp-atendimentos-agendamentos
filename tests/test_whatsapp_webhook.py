from httpx import AsyncClient
from pytest import mark


@mark.asyncio
async def test_webhook_verification_with_correct_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge-value",
        },
    )

    assert response.status_code == 200
    assert response.text == "challenge-value"


@mark.asyncio
async def test_webhook_verification_with_incorrect_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "incorrect-token",
            "hub.challenge": "challenge-value",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Webhook verification failed"}


@mark.asyncio
async def test_webhook_verification_with_invalid_parameters(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid request"

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import AsaasConfiguration


class AsaasGatewayError(RuntimeError):
    """Sanitized provider failure; never include provider payloads or secrets."""


@dataclass(frozen=True, slots=True)
class AsaasCheckoutResult:
    checkout_id: str
    checkout_url: str


class AsaasGateway:
    def __init__(self, configuration: AsaasConfiguration) -> None:
        self.configuration = configuration

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "User-Agent": "ALOVIA/1.0",
            "access_token": self.configuration.api_key.get_secret_value(),
        }

    async def create_checkout(self, payload: dict[str, Any]) -> AsaasCheckoutResult:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.configuration.api_base_url}/checkouts",
                    headers=self._headers(),
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AsaasGatewayError("Asaas checkout creation failed") from exc

        checkout_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(checkout_id, str) or not checkout_id.strip() or len(checkout_id) > 80:
            raise AsaasGatewayError("Asaas checkout response is invalid")

        raw_link = data.get("link") if isinstance(data, dict) else None
        if isinstance(raw_link, str) and self._safe_checkout_url(raw_link):
            checkout_url = raw_link
        else:
            checkout_url = (
                f"{self.configuration.checkout_base_url}/checkoutSession/show?id={checkout_id}"
            )
        return AsaasCheckoutResult(checkout_id=checkout_id, checkout_url=checkout_url)

    async def payments_for_checkout(self, checkout_id: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.configuration.api_base_url}/payments",
                    headers=self._headers(),
                    params={"checkoutSession": checkout_id, "limit": 20},
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AsaasGatewayError("Asaas payment reconciliation failed") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise AsaasGatewayError("Asaas payments response is invalid")
        return [row for row in data if isinstance(row, dict)]

    def _safe_checkout_url(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        expected = urlsplit(self.configuration.checkout_base_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == expected.hostname
            and parsed.username is None
            and parsed.password is None
        )

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.core.config import AsaasConfiguration


class AsaasGatewayError(RuntimeError):
    """Sanitized provider failure; never include provider payloads or secrets."""


@dataclass(frozen=True, slots=True)
class AsaasCheckoutResult:
    checkout_id: str
    checkout_url: str


@dataclass(frozen=True, slots=True)
class AsaasPixAuthorizationResult:
    authorization_id: str
    payload: str
    conciliation_identifier: str | None
    expires_at: datetime | None


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
        data = await self._json_request("POST", "/checkouts", json=payload)
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

    async def find_customer(self, *, external_reference: str, cpf_cnpj: str) -> str | None:
        payload = await self._json_request(
            "GET",
            "/customers",
            params={
                "externalReference": external_reference,
                "cpfCnpj": cpf_cnpj,
                "limit": 10,
            },
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AsaasGatewayError("Asaas customer lookup response is invalid")
        for row in rows:
            if not isinstance(row, dict):
                continue
            customer_id = row.get("id")
            if isinstance(customer_id, str) and customer_id.startswith("cus_") and len(customer_id) <= 80:
                return customer_id
        return None

    async def create_customer(
        self,
        *,
        name: str,
        cpf_cnpj: str,
        email: str,
        external_reference: str,
    ) -> str:
        payload = await self._json_request(
            "POST",
            "/customers",
            json={
                "name": name,
                "cpfCnpj": cpf_cnpj,
                "email": email,
                "externalReference": external_reference,
                "notificationDisabled": False,
            },
        )
        customer_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(customer_id, str) or not customer_id.startswith("cus_") or len(customer_id) > 80:
            raise AsaasGatewayError("Asaas customer response is invalid")
        return customer_id

    async def create_pix_authorization(
        self, payload: dict[str, Any]
    ) -> AsaasPixAuthorizationResult:
        data = await self._json_request(
            "POST", "/pix/automatic/authorizations", json=payload
        )
        authorization_id = data.get("id") if isinstance(data, dict) else None
        immediate = data.get("immediateQrCode") if isinstance(data, dict) else None
        if not isinstance(authorization_id, str) or not authorization_id.strip() or len(authorization_id) > 100:
            raise AsaasGatewayError("Asaas Pix Automatic response is invalid")
        if not isinstance(immediate, dict):
            raise AsaasGatewayError("Asaas Pix Automatic QR response is invalid")
        qr_payload = immediate.get("payload")
        if not isinstance(qr_payload, str) or not qr_payload.strip() or len(qr_payload) > 8_000:
            raise AsaasGatewayError("Asaas Pix Automatic payload is invalid")
        conciliation = immediate.get("conciliationIdentifier")
        if not isinstance(conciliation, str) or len(conciliation) > 100:
            conciliation = None
        expires_at = _parse_asaas_datetime(immediate.get("expirationDate"))
        return AsaasPixAuthorizationResult(
            authorization_id=authorization_id,
            payload=qr_payload,
            conciliation_identifier=conciliation,
            expires_at=expires_at,
        )

    async def payments_for_checkout(self, checkout_id: str) -> list[dict[str, Any]]:
        payload = await self._json_request(
            "GET",
            "/payments",
            params={"checkoutSession": checkout_id, "limit": 20},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise AsaasGatewayError("Asaas payments response is invalid")
        return [row for row in data if isinstance(row, dict)]

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(
                    method,
                    f"{self.configuration.api_base_url}{path}",
                    headers=self._headers(),
                    json=json,
                    params=params,
                )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AsaasGatewayError("Asaas request failed") from exc
        if not isinstance(data, dict):
            raise AsaasGatewayError("Asaas response is invalid")
        return data

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


def _parse_asaas_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T", 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    return parsed.astimezone(UTC)

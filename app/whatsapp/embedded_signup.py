from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.core.config import MetaEmbeddedSignupConfiguration
from app.whatsapp.administration import WhatsAppConnectionStatusView
from app.whatsapp.connections import WhatsAppConnectionMode
from app.whatsapp.credentials import WhatsAppCredentialStore
from app.whatsapp.onboarding import (
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingService,
    WhatsAppProviderCompletion,
)


class MetaEmbeddedSignupError(RuntimeError):
    """Falha sanitizada e recuperável na comunicação com a Meta."""


class MetaEmbeddedSignupRejected(MetaEmbeddedSignupError):
    """A autorização ou os ativos informados não foram confirmados pela Meta."""


class MetaEmbeddedSignupUnavailable(MetaEmbeddedSignupError):
    """A Meta não pôde ser consultada com segurança."""


@dataclass(frozen=True, slots=True)
class MetaAuthorizedAssets:
    access_token: SecretStr
    waba_id: str
    phone_number_id: str
    display_phone_number: str | None


class MetaEmbeddedSignupPort(Protocol):
    async def exchange_and_validate(
        self,
        authorization_code: SecretStr,
        *,
        waba_id_hint: str,
        phone_number_id_hint: str | None,
    ) -> MetaAuthorizedAssets: ...

    async def subscribe_app(
        self,
        assets: MetaAuthorizedAssets,
    ) -> None: ...


class MetaEmbeddedSignupGateway:
    """Troca o código e confirma WABA/telefone via Graph API no servidor."""

    def __init__(
        self,
        configuration: MetaEmbeddedSignupConfiguration,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._configuration = configuration
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=(
                f"https://graph.facebook.com/{configuration.graph_version}/"
            ),
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def exchange_and_validate(
        self,
        authorization_code: SecretStr,
        *,
        waba_id_hint: str,
        phone_number_id_hint: str | None,
    ) -> MetaAuthorizedAssets:
        waba_id = _numeric_meta_id(waba_id_hint)
        phone_number_id = (
            _numeric_meta_id(phone_number_id_hint)
            if phone_number_id_hint is not None
            else None
        )
        code = authorization_code.get_secret_value().strip()
        if not code or len(code) > 4096:
            raise MetaEmbeddedSignupRejected(
                "Meta authorization is invalid"
            )

        token_payload = await self._request_json(
            "GET",
            "oauth/access_token",
            params={
                "client_id": self._configuration.app_id,
                "client_secret": (
                    self._configuration.app_secret.get_secret_value()
                ),
                "code": code,
            },
        )
        raw_token = token_payload.get("access_token")
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise MetaEmbeddedSignupRejected(
                "Meta authorization is invalid"
            )
        access_token = SecretStr(raw_token.strip())
        headers = {
            "Authorization": f"Bearer {access_token.get_secret_value()}"
        }

        waba_payload = await self._request_json(
            "GET",
            waba_id,
            headers=headers,
            params={"fields": "id"},
        )
        if str(waba_payload.get("id", "")) != waba_id:
            raise MetaEmbeddedSignupRejected(
                "Meta business account is invalid"
            )

        phones_payload = await self._request_json(
            "GET",
            f"{waba_id}/phone_numbers",
            headers=headers,
            params={"fields": "id,display_phone_number", "limit": "100"},
        )
        phones = _phone_assets(phones_payload)
        if phone_number_id is None:
            if len(phones) != 1:
                raise MetaEmbeddedSignupRejected(
                    "Meta phone number selection is ambiguous"
                )
            selected_id, display_phone_number = next(iter(phones.items()))
        else:
            display_phone_number = phones.get(phone_number_id)
            if phone_number_id not in phones:
                raise MetaEmbeddedSignupRejected(
                    "Meta phone number is invalid"
                )
            selected_id = phone_number_id

        return MetaAuthorizedAssets(
            access_token=access_token,
            waba_id=waba_id,
            phone_number_id=selected_id,
            display_phone_number=display_phone_number,
        )

    async def subscribe_app(self, assets: MetaAuthorizedAssets) -> None:
        payload = await self._request_json(
            "POST",
            f"{assets.waba_id}/subscribed_apps",
            headers={
                "Authorization": (
                    f"Bearer {assets.access_token.get_secret_value()}"
                )
            },
        )
        if payload.get("success") is not True:
            raise MetaEmbeddedSignupRejected(
                "Meta webhook subscription was not confirmed"
            )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                params=params,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            raise MetaEmbeddedSignupUnavailable(
                "Meta is temporarily unavailable"
            ) from None
        except httpx.HTTPError:
            raise MetaEmbeddedSignupUnavailable(
                "Meta request failed"
            ) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise MetaEmbeddedSignupUnavailable(
                "Meta is temporarily unavailable"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise MetaEmbeddedSignupRejected(
                "Meta authorization was rejected"
            )
        try:
            payload = response.json()
        except ValueError:
            raise MetaEmbeddedSignupUnavailable(
                "Meta response is invalid"
            ) from None
        if not isinstance(payload, dict):
            raise MetaEmbeddedSignupUnavailable(
                "Meta response is invalid"
            )
        return payload


class MetaEmbeddedSignupService:
    def __init__(
        self,
        onboarding: WhatsAppOnboardingService,
        gateway: MetaEmbeddedSignupPort,
        credential_store: WhatsAppCredentialStore,
        graph_version: str,
    ) -> None:
        self._onboarding = onboarding
        self._gateway = gateway
        self._credential_store = credential_store
        self._graph_version = graph_version

    async def complete_coexistence(
        self,
        business_id: uuid.UUID,
        authorization_code: SecretStr,
        *,
        waba_id_hint: str,
        phone_number_id_hint: str | None,
    ) -> WhatsAppConnectionStatusView:
        assets = await self._gateway.exchange_and_validate(
            authorization_code,
            waba_id_hint=waba_id_hint,
            phone_number_id_hint=phone_number_id_hint,
        )
        await self._gateway.subscribe_app(assets)
        credential_secret_ref = await self._credential_store.store(
            business_id,
            assets.access_token,
        )
        return await self._onboarding.complete_provider_onboarding(
            business_id,
            WhatsAppProviderCompletion(
                intent=WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
                confirmed_mode=WhatsAppConnectionMode.COEXISTENCE,
                meta_waba_id=assets.waba_id,
                meta_phone_number_id=assets.phone_number_id,
                graph_version=self._graph_version,
                credential_secret_ref=credential_secret_ref,
                provider_confirmed=True,
                display_phone_number=assets.display_phone_number,
            ),
        )


def _numeric_meta_id(value: str | None) -> str:
    if value is None:
        raise MetaEmbeddedSignupRejected("Meta identifier is missing")
    normalized = value.strip()
    if not normalized.isdigit() or len(normalized) > 32:
        raise MetaEmbeddedSignupRejected("Meta identifier is invalid")
    return normalized


def _phone_assets(payload: dict[str, Any]) -> dict[str, str | None]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise MetaEmbeddedSignupUnavailable("Meta response is invalid")
    phones: dict[str, str | None] = {}
    for raw_phone in data:
        if not isinstance(raw_phone, dict):
            continue
        raw_id = raw_phone.get("id")
        if not isinstance(raw_id, str):
            continue
        try:
            phone_id = _numeric_meta_id(raw_id)
        except MetaEmbeddedSignupRejected:
            continue
        display = raw_phone.get("display_phone_number")
        phones[phone_id] = (
            display.strip()
            if isinstance(display, str) and 1 <= len(display.strip()) <= 64
            else None
        )
    if not phones:
        raise MetaEmbeddedSignupRejected("Meta phone number is unavailable")
    return phones

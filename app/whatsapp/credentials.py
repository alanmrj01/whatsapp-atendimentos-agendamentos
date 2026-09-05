from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from pydantic import SecretStr

from app.core.config import Settings
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.connections import WhatsAppConnectionRecord


class WhatsAppCredentialProvider(Protocol):
    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr: ...


class WhatsAppCredentialStore(Protocol):
    async def store(
        self,
        business_id: uuid.UUID,
        credential: SecretStr,
    ) -> str: ...


class GlobalSettingsCredentialProvider:
    """Fallback LEGACY/PILOT, limitado ao Phone Number ID global."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr:
        if connection.credential_secret_ref is not None:
            raise WhatsAppConfigurationError(
                "WhatsApp credential provider is not available"
            )

        token = self._settings.meta_access_token
        global_phone_number_id = self._settings.meta_phone_number_id
        if (
            token is None
            or not token.get_secret_value().strip()
            or connection.meta_phone_number_id is None
            or connection.meta_phone_number_id != global_phone_number_id
        ):
            raise WhatsAppConfigurationError(
                "WhatsApp credential is not configured"
            )
        return token


class GoogleSecretManagerCredentialProvider:
    """Resolve somente referências versionadas; nunca persiste token no banco."""

    def __init__(self, client: object | None = None) -> None:
        self._secret_manager_client = client

    def _client(self) -> object:
        if self._secret_manager_client is None:
            from google.cloud import secretmanager

            self._secret_manager_client = secretmanager.SecretManagerServiceClient()
        return self._secret_manager_client

    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr:
        reference = connection.credential_secret_ref
        if reference is None:
            raise WhatsAppConfigurationError(
                "WhatsApp credential is not configured"
            )
        try:
            response = await asyncio.to_thread(
                self._client().access_secret_version,
                request={"name": reference},
            )
            value = response.payload.data.decode("utf-8").strip()
        except Exception:
            raise WhatsAppConfigurationError(
                "WhatsApp credential is not available"
            ) from None
        if not value:
            raise WhatsAppConfigurationError(
                "WhatsApp credential is not available"
            )
        return SecretStr(value)


class GoogleSecretManagerCredentialStore:
    """Cria uma versão imutável por onboarding e retorna só sua referência."""

    def __init__(self, project_id: str, client: object | None = None) -> None:
        self._project_id = project_id
        self._secret_manager_client = client

    def _client(self) -> object:
        if self._secret_manager_client is None:
            from google.cloud import secretmanager

            self._secret_manager_client = secretmanager.SecretManagerServiceClient()
        return self._secret_manager_client

    async def store(
        self,
        business_id: uuid.UUID,
        credential: SecretStr,
    ) -> str:
        from google.api_core.exceptions import AlreadyExists

        parent = f"projects/{self._project_id}"
        secret_id = f"alovia-whatsapp-{business_id.hex}"
        secret_name = f"{parent}/secrets/{secret_id}"
        client = self._client()
        try:
            await asyncio.to_thread(
                client.create_secret,
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                },
            )
        except AlreadyExists:
            pass
        except Exception:
            raise WhatsAppConfigurationError(
                "WhatsApp credential could not be stored"
            ) from None

        try:
            response = await asyncio.to_thread(
                client.add_secret_version,
                request={
                    "parent": secret_name,
                    "payload": {
                        "data": credential.get_secret_value().encode("utf-8")
                    },
                },
            )
            reference = str(response.name)
        except Exception:
            raise WhatsAppConfigurationError(
                "WhatsApp credential could not be stored"
            ) from None
        return reference


class BusinessWhatsAppCredentialProvider:
    """Secret Manager por conexão; global apenas para o piloto legado explícito."""

    def __init__(
        self,
        settings: Settings,
        *,
        secret_manager: WhatsAppCredentialProvider | None = None,
    ) -> None:
        self._legacy = GlobalSettingsCredentialProvider(settings)
        self._secret_manager = (
            secret_manager or GoogleSecretManagerCredentialProvider()
        )

    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr:
        if connection.credential_secret_ref is not None:
            return await self._secret_manager.resolve(connection)
        return await self._legacy.resolve(connection)

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from app.core.config import Settings
from app.whatsapp.client import WhatsAppConfigurationError
from app.whatsapp.connections import WhatsAppConnectionRecord


class WhatsAppCredentialProvider(Protocol):
    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr: ...


class GlobalSettingsCredentialProvider:
    """Fallback LEGACY/PILOT; Secret Manager por empresa virá no onboarding."""

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

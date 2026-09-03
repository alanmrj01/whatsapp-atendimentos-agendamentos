from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from app.core.config import Settings
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.whatsapp.client import (
    WhatsAppClient,
    WhatsAppClientConfiguration,
    WhatsAppConfigurationError,
)
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionRecord,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
)
from app.whatsapp.credentials import (
    GlobalSettingsCredentialProvider,
    WhatsAppCredentialProvider,
)


class ConnectionLookup(Protocol):
    async def get_connection_record(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionRecord | None: ...

    async def get_legacy_phone_number_id(
        self, business_id: uuid.UUID
    ) -> str | None: ...


class Sender(Protocol):
    async def send_text(self, to: str, text: str) -> str: ...

    async def send_interactive_buttons(
        self,
        to: str,
        body: str,
        buttons: Sequence[Mapping[str, Any]],
    ) -> str: ...

    async def send_interactive_list(
        self,
        to: str,
        body: str,
        sections: Sequence[Mapping[str, Any]],
    ) -> str: ...

    async def aclose(self) -> None: ...


ClientBuilder = Callable[[WhatsAppClientConfiguration], Sender]


class BusinessWhatsAppSenderResolver:
    def __init__(
        self,
        repository: ConnectionLookup,
        settings: Settings,
        *,
        credential_provider: WhatsAppCredentialProvider | None = None,
        client_builder: ClientBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._credential_provider = (
            credential_provider or GlobalSettingsCredentialProvider(settings)
        )
        self._client_builder = client_builder or (
            lambda configuration: WhatsAppClient(configuration=configuration)
        )

    async def resolve(self, business_id: uuid.UUID) -> Sender:
        connection = await self._repository.get_connection_record(business_id)
        if connection is None:
            connection = await self._legacy_pilot_connection(business_id)
        elif connection.business_id != business_id:
            raise WhatsAppConfigurationError(
                "WhatsApp business connection is inconsistent"
            )

        if (
            connection.provider is not WhatsAppProvider.META
            or connection.status is not WhatsAppConnectionStatus.CONNECTED
            or connection.meta_phone_number_id is None
        ):
            raise WhatsAppConfigurationError(
                "WhatsApp business connection is not available"
            )

        graph_version = connection.graph_version or self._settings.meta_graph_version
        if graph_version is None:
            raise WhatsAppConfigurationError(
                "WhatsApp Graph version is not configured"
            )
        access_token = await self._credential_provider.resolve(connection)
        configuration = WhatsAppClientConfiguration(
            access_token=access_token,
            phone_number_id=connection.meta_phone_number_id,
            graph_version=graph_version,
        )
        return self._client_builder(configuration)

    async def _legacy_pilot_connection(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionRecord:
        legacy_phone_number_id = (
            await self._repository.get_legacy_phone_number_id(business_id)
        )
        if (
            legacy_phone_number_id is None
            or legacy_phone_number_id != self._settings.meta_phone_number_id
        ):
            raise WhatsAppConfigurationError(
                "WhatsApp business connection is not configured"
            )
        return WhatsAppConnectionRecord(
            id=uuid.UUID(int=0),
            business_id=business_id,
            provider=WhatsAppProvider.META,
            mode=WhatsAppConnectionMode.API_ONLY,
            status=WhatsAppConnectionStatus.CONNECTED,
            meta_waba_id=self._settings.meta_waba_id,
            meta_phone_number_id=legacy_phone_number_id,
            credential_secret_ref=None,
            graph_version=self._settings.meta_graph_version,
        )


def build_business_sender_resolver(
    repository: WhatsAppConnectionRepository,
    settings: Settings,
) -> BusinessWhatsAppSenderResolver:
    return BusinessWhatsAppSenderResolver(repository, settings)

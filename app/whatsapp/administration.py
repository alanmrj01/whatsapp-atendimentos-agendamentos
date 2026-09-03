from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, BusinessWhatsAppConnection
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
    sanitize_error_code,
    validate_credential_secret_ref,
    validate_graph_version,
    validate_meta_identifier,
)


class WhatsAppConnectionAdministrationError(RuntimeError):
    """Falha sanitizada ao administrar uma conexão WhatsApp."""


@dataclass(frozen=True, slots=True)
class WhatsAppConnectionStatusView:
    id: uuid.UUID
    business_id: uuid.UUID
    provider: WhatsAppProvider
    mode: WhatsAppConnectionMode
    status: WhatsAppConnectionStatus
    has_phone_number_id: bool
    has_credential_reference: bool
    connected_at: datetime | None
    disconnected_at: datetime | None
    last_error_code: str | None


class WhatsAppConnectionAdministrationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = WhatsAppConnectionRepository(session)

    async def create_pending_connection(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView:
        normalized_mode = _validated_mode(mode)
        business_exists = await self._session.scalar(
            select(Business.id).where(Business.id == business_id)
        )
        if business_exists is None:
            raise WhatsAppConnectionAdministrationError(
                "Business is not available"
            )
        current = await self._repository.get_connection(
            business_id, for_update=True
        )
        if current is not None and current.status != (
            WhatsAppConnectionStatus.DISCONNECTED.value
        ):
            raise WhatsAppConnectionAdministrationError(
                "Business already has an active WhatsApp connection"
            )
        connection = BusinessWhatsAppConnection(
            business_id=business_id,
            provider=WhatsAppProvider.META.value,
            mode=normalized_mode.value,
            status=WhatsAppConnectionStatus.PENDING.value,
        )
        self._session.add(connection)
        await self._session.flush()
        return _status_view(connection)

    async def get_connection(
        self,
        business_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WhatsAppConnectionStatusView | None:
        connection = await self._repository.get_connection(
            business_id, for_update=for_update
        )
        if connection is None:
            return None
        return _status_view(connection)

    async def update_meta_identifiers(
        self,
        business_id: uuid.UUID,
        *,
        meta_waba_id: str | None,
        meta_phone_number_id: str | None,
        display_phone_number: str | None = None,
        graph_version: str | None = None,
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        try:
            connection.meta_waba_id = validate_meta_identifier(meta_waba_id)
            normalized_phone_number_id = validate_meta_identifier(
                meta_phone_number_id
            )
            normalized_graph_version = validate_graph_version(graph_version)
        except ValueError:
            raise WhatsAppConnectionAdministrationError(
                "Meta connection identifiers are invalid"
            ) from None
        if normalized_phone_number_id is not None:
            legacy_owner = await self._session.scalar(
                select(Business.id).where(
                    Business.meta_phone_number_id == normalized_phone_number_id,
                    Business.id != business_id,
                )
            )
            if legacy_owner is not None:
                raise WhatsAppConnectionAdministrationError(
                    "Meta Phone Number ID belongs to another business"
                )
        connection.meta_phone_number_id = normalized_phone_number_id
        if display_phone_number is not None and not (
            1 <= len(display_phone_number) <= 64
        ):
            raise WhatsAppConnectionAdministrationError(
                "Display phone number is invalid"
            )
        connection.display_phone_number = display_phone_number
        connection.graph_version = normalized_graph_version
        await self._session.flush()
        return _status_view(connection)

    async def mark_connected(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        if any(
            value is None
            for value in (
                connection.meta_waba_id,
                connection.meta_phone_number_id,
                connection.graph_version,
                connection.credential_secret_ref,
            )
        ):
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp connection configuration is incomplete"
            )
        connection.status = WhatsAppConnectionStatus.CONNECTED.value
        connection.connected_at = datetime.now(timezone.utc)
        connection.disconnected_at = None
        connection.last_error_code = None
        await self._session.flush()
        return _status_view(connection)

    async def mark_disconnected(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        connection.status = WhatsAppConnectionStatus.DISCONNECTED.value
        connection.disconnected_at = datetime.now(timezone.utc)
        await self._session.flush()
        return _status_view(connection)

    async def set_credential_secret_ref(
        self,
        business_id: uuid.UUID,
        credential_secret_ref: str,
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        try:
            connection.credential_secret_ref = validate_credential_secret_ref(
                credential_secret_ref
            )
        except ValueError:
            raise WhatsAppConnectionAdministrationError(
                "Credential secret reference is invalid"
            ) from None
        await self._session.flush()
        return _status_view(connection)

    async def change_mode(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        if connection.status == WhatsAppConnectionStatus.CONNECTED.value:
            raise WhatsAppConnectionAdministrationError(
                "Connected WhatsApp mode cannot be changed"
            )
        connection.mode = _validated_mode(mode).value
        await self._session.flush()
        return _status_view(connection)

    async def _require_connection(
        self, business_id: uuid.UUID
    ) -> BusinessWhatsAppConnection:
        connection = await self._repository.get_connection(
            business_id, for_update=True
        )
        if connection is None or connection.business_id != business_id:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp business connection is not available"
            )
        return connection


def _status_view(
    connection: BusinessWhatsAppConnection,
) -> WhatsAppConnectionStatusView:
    return WhatsAppConnectionStatusView(
        id=connection.id,
        business_id=connection.business_id,
        provider=WhatsAppProvider(connection.provider),
        mode=WhatsAppConnectionMode(connection.mode),
        status=WhatsAppConnectionStatus(connection.status),
        has_phone_number_id=connection.meta_phone_number_id is not None,
        has_credential_reference=connection.credential_secret_ref is not None,
        connected_at=connection.connected_at,
        disconnected_at=connection.disconnected_at,
        last_error_code=sanitize_error_code(connection.last_error_code),
    )


def _validated_mode(mode: WhatsAppConnectionMode) -> WhatsAppConnectionMode:
    try:
        return WhatsAppConnectionMode(mode)
    except ValueError:
        raise WhatsAppConnectionAdministrationError(
            "WhatsApp connection mode is invalid"
        ) from None

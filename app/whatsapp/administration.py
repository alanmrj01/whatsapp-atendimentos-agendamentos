from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, BusinessWhatsAppConnection, WebPushEvent
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
    masked_display_phone_number: str | None = None


META_ONBOARDING_PENDING = "META_ONBOARDING_PENDING"


class WhatsAppConnectionAdministrationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = WhatsAppConnectionRepository(session)

    async def begin_pending_connection(
        self,
        business_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
    ) -> WhatsAppConnectionStatusView:
        normalized_mode = _validated_mode(mode)
        current = await self._repository.get_connection(
            business_id, for_update=True
        )
        if current is None or current.status == WhatsAppConnectionStatus.DISCONNECTED.value:
            view = await self.create_pending_connection(
                business_id, normalized_mode
            )
            connection = await self._require_connection(business_id)
            connection.last_error_code = META_ONBOARDING_PENDING
            await self._session.flush()
            return _status_view(connection)

        if current.status == WhatsAppConnectionStatus.CONNECTED.value:
            raise WhatsAppConnectionAdministrationError(
                "Business already has an active WhatsApp connection"
            )
        if current.mode != normalized_mode.value:
            raise WhatsAppConnectionAdministrationError(
                "Pending WhatsApp connection uses a different mode"
            )

        if current.status == WhatsAppConnectionStatus.ERROR.value:
            current.meta_waba_id = None
            current.meta_phone_number_id = None
            current.display_phone_number = None
            current.credential_secret_ref = None
            current.graph_version = None
        current.status = WhatsAppConnectionStatus.PENDING.value
        current.disconnected_at = None
        current.last_error_code = META_ONBOARDING_PENDING
        await self._session.flush()
        return _status_view(current)

    async def record_pending_meta_assets(
        self,
        business_id: uuid.UUID,
        *,
        meta_waba_id: str,
        meta_phone_number_id: str | None,
        graph_version: str,
    ) -> WhatsAppConnectionStatusView:
        current = await self._repository.get_connection(
            business_id, for_update=True
        )
        if current is None or current.status != WhatsAppConnectionStatus.PENDING.value:
            raise WhatsAppConnectionAdministrationError(
                "Pending WhatsApp connection is not available"
            )
        return await self.update_meta_identifiers(
            business_id,
            meta_waba_id=meta_waba_id,
            meta_phone_number_id=meta_phone_number_id,
            graph_version=graph_version,
        )

    async def record_meta_review_decision(
        self,
        meta_waba_id: str,
        decision: str,
    ) -> WhatsAppConnectionStatusView | None:
        normalized_waba_id = validate_meta_identifier(meta_waba_id)
        if normalized_waba_id is None:
            return None
        connection = await self._repository.get_active_connection_by_waba_id(
            normalized_waba_id,
            for_update=True,
        )
        if connection is None:
            return None

        normalized_decision = decision.strip().upper()
        if normalized_decision not in {"APPROVED", "REJECTED", "DECLINED"}:
            return _status_view(connection)

        # Meta review is stored independently from the technical connection.
        # It must never flip connected/pending/error on its own.
        if isinstance(self._session, AsyncSession):
            business = await self._session.get(Business, connection.business_id)
            if business is not None:
                business.whatsapp_review_status = (
                    "approved" if normalized_decision == "APPROVED" else "rejected"
                )
                business.whatsapp_review_checked_at = datetime.now(timezone.utc)
                if (
                    normalized_decision == "APPROVED"
                    and business.whatsapp_desired_mode == WhatsAppConnectionMode.COEXISTENCE.value
                    and connection.mode == WhatsAppConnectionMode.API_ONLY.value
                ):
                    await self._session.execute(
                        postgresql_insert(WebPushEvent)
                        .values(
                            id=uuid.uuid4(),
                            business_id=business.id,
                            event_key=f"whatsapp-review-approved:{business.id}:{normalized_waba_id}",
                            event_type="whatsapp_coexistence_ready",
                            target_path="/app/whatsapp?alterar=coexistence",
                        )
                        .on_conflict_do_nothing(
                            constraint="uq_web_push_events_event_key"
                        )
                    )
        return _status_view(connection)

    async def update_business_preferences(
        self,
        business_id: uuid.UUID,
        *,
        desired_mode: WhatsAppConnectionMode,
        setup_source: str | None,
    ) -> None:
        business = await self._session.get(Business, business_id)
        if business is None:
            raise WhatsAppConnectionAdministrationError("Business is not available")
        if setup_source not in {None, "business_app", "migrated_to_business", "exclusive"}:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp setup source is invalid"
            )
        business.whatsapp_desired_mode = _validated_mode(desired_mode).value
        business.whatsapp_setup_source = setup_source
        if desired_mode is WhatsAppConnectionMode.COEXISTENCE:
            business.whatsapp_review_status = business.whatsapp_review_status or "unknown"
        await self._session.flush()

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
        # A disconnected record must no longer be usable for inbound/outbound
        # traffic and must not prevent the same number from being connected again.
        connection.meta_waba_id = None
        connection.meta_phone_number_id = None
        connection.display_phone_number = None
        connection.credential_secret_ref = None
        connection.graph_version = None
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

    async def transition_connected_connection(
        self,
        business_id: uuid.UUID,
        *,
        mode: WhatsAppConnectionMode,
        meta_waba_id: str,
        meta_phone_number_id: str,
        display_phone_number: str | None,
        graph_version: str,
        credential_secret_ref: str,
    ) -> WhatsAppConnectionStatusView:
        connection = await self._require_connection(business_id)
        if connection.status != WhatsAppConnectionStatus.CONNECTED.value:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp connection is not active"
            )
        if connection.mode == mode.value:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp connection already uses this mode"
            )
        try:
            next_waba = validate_meta_identifier(meta_waba_id)
            next_phone = validate_meta_identifier(meta_phone_number_id)
            next_graph = validate_graph_version(graph_version)
            next_secret = validate_credential_secret_ref(credential_secret_ref)
        except ValueError:
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp transition configuration is invalid"
            ) from None
        if (
            next_waba is None
            or next_phone is None
            or next_graph is None
            or next_secret is None
            or connection.meta_phone_number_id != next_phone
        ):
            raise WhatsAppConnectionAdministrationError(
                "WhatsApp transition must keep the same phone number"
            )
        connection.mode = _validated_mode(mode).value
        connection.meta_waba_id = next_waba
        connection.meta_phone_number_id = next_phone
        connection.display_phone_number = display_phone_number
        connection.graph_version = next_graph
        connection.credential_secret_ref = next_secret
        connection.connected_at = datetime.now(timezone.utc)
        connection.disconnected_at = None
        connection.last_error_code = None
        await self._session.flush()
        return _status_view(connection)

    async def connection_by_waba(
        self,
        meta_waba_id: str,
    ) -> WhatsAppConnectionStatusView | None:
        try:
            normalized = validate_meta_identifier(meta_waba_id)
        except ValueError:
            return None
        if normalized is None:
            return None
        connection = await self._repository.get_active_connection_by_waba_id(
            normalized
        )
        return _status_view(connection) if connection is not None else None

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
        masked_display_phone_number=_mask_display_phone_number(
            connection.display_phone_number
        ),
    )


def _mask_display_phone_number(value: str | None) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) < 4:
        return None
    return f"•••• {digits[-4:]}"


def _validated_mode(mode: WhatsAppConnectionMode) -> WhatsAppConnectionMode:
    try:
        return WhatsAppConnectionMode(mode)
    except ValueError:
        raise WhatsAppConnectionAdministrationError(
            "WhatsApp connection mode is invalid"
        ) from None

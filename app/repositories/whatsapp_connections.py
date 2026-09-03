from __future__ import annotations

import uuid

from sqlalchemy import case, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, BusinessWhatsAppConnection
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionRecord,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
)


class WhatsAppConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_connection(
        self,
        business_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> BusinessWhatsAppConnection | None:
        statement = (
            select(BusinessWhatsAppConnection)
            .where(BusinessWhatsAppConnection.business_id == business_id)
            .order_by(
                case(
                    (
                        BusinessWhatsAppConnection.status
                        != WhatsAppConnectionStatus.DISCONNECTED.value,
                        0,
                    ),
                    else_=1,
                ),
                BusinessWhatsAppConnection.created_at.desc(),
                BusinessWhatsAppConnection.id.desc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    async def get_connection_record(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionRecord | None:
        connection = await self.get_connection(business_id)
        if connection is None:
            return None
        return connection_record(connection)

    async def get_legacy_phone_number_id(
        self, business_id: uuid.UUID
    ) -> str | None:
        return await self.session.scalar(
            select(Business.meta_phone_number_id).where(
                Business.id == business_id
            )
        )

    async def find_business_id_by_phone_number_id(
        self, meta_phone_number_id: str
    ) -> uuid.UUID | None:
        legacy_business_id = await self.session.scalar(
            select(Business.id).where(
                Business.meta_phone_number_id == meta_phone_number_id
            )
        )
        connection_business_id = await self.session.scalar(
            select(BusinessWhatsAppConnection.business_id).where(
                BusinessWhatsAppConnection.meta_phone_number_id
                == meta_phone_number_id,
                BusinessWhatsAppConnection.status
                == WhatsAppConnectionStatus.CONNECTED.value,
            )
        )
        if connection_business_id is not None:
            if (
                legacy_business_id is not None
                and legacy_business_id != connection_business_id
            ):
                return None
            return connection_business_id
        known_connection = await self.session.scalar(
            select(BusinessWhatsAppConnection.id).where(
                BusinessWhatsAppConnection.meta_phone_number_id
                == meta_phone_number_id
            )
        )
        if known_connection is not None:
            return None
        if legacy_business_id is None:
            return None
        return await self.session.scalar(
            select(Business.id).where(
                Business.id == legacy_business_id,
                ~exists(
                    select(BusinessWhatsAppConnection.id).where(
                        BusinessWhatsAppConnection.business_id == Business.id
                    )
                ),
            )
        )


def connection_record(
    connection: BusinessWhatsAppConnection,
) -> WhatsAppConnectionRecord:
    return WhatsAppConnectionRecord(
        id=connection.id,
        business_id=connection.business_id,
        provider=WhatsAppProvider(connection.provider),
        mode=WhatsAppConnectionMode(connection.mode),
        status=WhatsAppConnectionStatus(connection.status),
        meta_waba_id=connection.meta_waba_id,
        meta_phone_number_id=connection.meta_phone_number_id,
        credential_secret_ref=connection.credential_secret_ref,
        graph_version=connection.graph_version,
        connected_at=connection.connected_at,
        disconnected_at=connection.disconnected_at,
        last_error_code=connection.last_error_code,
    )

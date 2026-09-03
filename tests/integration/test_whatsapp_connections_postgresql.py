from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.models import Business, BusinessWhatsAppConnection
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.whatsapp.administration import (
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.client import WhatsAppClientConfiguration
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionRecord,
    WhatsAppConnectionStatus,
)
from app.whatsapp.sender import BusinessWhatsAppSenderResolver
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL,
    _async_url,
    migrated_test_database,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
    ),
]

assert migrated_test_database


@pytest_asyncio.fixture
async def connection_sessions() -> AsyncIterator[
    async_sessionmaker[AsyncSession]
]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await session.execute(delete(BusinessWhatsAppConnection))
            await session.execute(delete(Business))
    yield factory
    await engine.dispose()


async def add_businesses(
    factory: async_sessionmaker[AsyncSession],
    *business_ids: uuid.UUID,
) -> None:
    async with factory() as session:
        async with session.begin():
            for index, business_id in enumerate(business_ids):
                session.add(
                    Business(
                        id=business_id,
                        name=f"WhatsApp connection test {index}",
                    )
                )


async def test_active_connection_and_phone_uniques_are_physical(
    connection_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_a = uuid.uuid4()
    business_b = uuid.uuid4()
    await add_businesses(connection_sessions, business_a, business_b)

    async with connection_sessions() as session:
        async with session.begin():
            session.add(
                BusinessWhatsAppConnection(
                    business_id=business_a,
                    mode=WhatsAppConnectionMode.API_ONLY.value,
                    status=WhatsAppConnectionStatus.PENDING.value,
                    meta_phone_number_id="physical-phone-a",
                )
            )

        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    BusinessWhatsAppConnection(
                        business_id=business_a,
                        mode=WhatsAppConnectionMode.COEXISTENCE.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                    )
                )
                await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    BusinessWhatsAppConnection(
                        business_id=business_b,
                        mode=WhatsAppConnectionMode.API_ONLY.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                        meta_phone_number_id="physical-phone-a",
                    )
                )
                await session.flush()


async def test_webhook_lookup_prioritizes_connected_model_and_guards_fallback(
    connection_sessions: async_sessionmaker[AsyncSession],
) -> None:
    connection_business = uuid.uuid4()
    legacy_business = uuid.uuid4()
    disconnected_business = uuid.uuid4()
    ambiguous_connection_business = uuid.uuid4()
    ambiguous_legacy_business = uuid.uuid4()
    async with connection_sessions() as session:
        async with session.begin():
            session.add_all(
                [
                    Business(
                        id=connection_business,
                        name="Connection owner",
                        meta_phone_number_id="connected-phone",
                    ),
                    Business(
                        id=legacy_business,
                        name="Legacy owner",
                        meta_phone_number_id="legacy-only-phone",
                    ),
                    Business(
                        id=disconnected_business,
                        name="Disconnected owner",
                        meta_phone_number_id="disconnected-phone",
                    ),
                    Business(
                        id=ambiguous_connection_business,
                        name="Ambiguous connection owner",
                    ),
                    Business(
                        id=ambiguous_legacy_business,
                        name="Ambiguous legacy owner",
                        meta_phone_number_id="ambiguous-phone",
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    BusinessWhatsAppConnection(
                        business_id=connection_business,
                        mode=WhatsAppConnectionMode.COEXISTENCE.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                        meta_phone_number_id="connected-phone",
                    ),
                    BusinessWhatsAppConnection(
                        business_id=disconnected_business,
                        mode=WhatsAppConnectionMode.API_ONLY.value,
                        status=WhatsAppConnectionStatus.DISCONNECTED.value,
                        meta_phone_number_id="disconnected-phone",
                    ),
                    BusinessWhatsAppConnection(
                        business_id=ambiguous_connection_business,
                        mode=WhatsAppConnectionMode.API_ONLY.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                        meta_phone_number_id="ambiguous-phone",
                    ),
                ]
            )

        repository = WhatsAppConnectionRepository(session)
        assert (
            await repository.find_business_id_by_phone_number_id(
                "connected-phone"
            )
            == connection_business
        )
        assert (
            await repository.find_business_id_by_phone_number_id(
                "legacy-only-phone"
            )
            == legacy_business
        )
        assert (
            await repository.find_business_id_by_phone_number_id(
                "disconnected-phone"
            )
            is None
        )
        assert (
            await repository.find_business_id_by_phone_number_id(
                "ambiguous-phone"
            )
            is None
        )


async def test_administration_service_controls_transitions_and_safe_status(
    connection_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    await add_businesses(connection_sessions, business_id)

    async with connection_sessions() as session:
        async with session.begin():
            service = WhatsAppConnectionAdministrationService(session)
            pending = await service.create_pending_connection(
                business_id,
                WhatsAppConnectionMode.COEXISTENCE,
            )
            assert pending.status is WhatsAppConnectionStatus.PENDING

            configured = await service.update_meta_identifiers(
                business_id,
                meta_waba_id="physical-waba",
                meta_phone_number_id="physical-admin-phone",
                display_phone_number="display-value-not-returned",
                graph_version="v23.0",
            )
            assert configured.has_phone_number_id is True
            assert not hasattr(configured, "display_phone_number")

            referenced = await service.set_credential_secret_ref(
                business_id,
                "projects/project-a/secrets/whatsapp-token/versions/latest",
            )
            assert referenced.has_credential_reference is True

            connected = await service.mark_connected(business_id)
            assert connected.status is WhatsAppConnectionStatus.CONNECTED
            with pytest.raises(WhatsAppConnectionAdministrationError):
                await service.change_mode(
                    business_id,
                    WhatsAppConnectionMode.API_ONLY,
                )

            disconnected = await service.mark_disconnected(business_id)
            assert disconnected.status is WhatsAppConnectionStatus.DISCONNECTED
            replacement = await service.create_pending_connection(
                business_id,
                WhatsAppConnectionMode.API_ONLY,
            )
            assert replacement.status is WhatsAppConnectionStatus.PENDING


class PhysicalCredentialProvider:
    def __init__(self, credentials: dict[uuid.UUID, str]) -> None:
        self.credentials = credentials

    async def resolve(
        self, connection: WhatsAppConnectionRecord
    ) -> SecretStr:
        return SecretStr(self.credentials[connection.business_id])


class PhysicalSender:
    async def aclose(self) -> None:
        return None


async def test_real_repository_resolves_each_business_to_its_own_sender(
    connection_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_a = uuid.uuid4()
    business_b = uuid.uuid4()
    await add_businesses(connection_sessions, business_a, business_b)
    async with connection_sessions() as session:
        async with session.begin():
            session.add_all(
                [
                    BusinessWhatsAppConnection(
                        business_id=business_a,
                        mode=WhatsAppConnectionMode.API_ONLY.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                        meta_phone_number_id="physical-sender-a",
                        graph_version="v23.0",
                    ),
                    BusinessWhatsAppConnection(
                        business_id=business_b,
                        mode=WhatsAppConnectionMode.COEXISTENCE.value,
                        status=WhatsAppConnectionStatus.CONNECTED.value,
                        meta_phone_number_id="physical-sender-b",
                        graph_version="v24.0",
                    ),
                ]
            )

        configurations: list[WhatsAppClientConfiguration] = []
        resolver = BusinessWhatsAppSenderResolver(
            WhatsAppConnectionRepository(session),
            Settings(_env_file=None),
            credential_provider=PhysicalCredentialProvider(
                {business_a: "credential-a", business_b: "credential-b"}
            ),
            client_builder=lambda configuration: (
                configurations.append(configuration) or PhysicalSender()
            ),
        )

        await resolver.resolve(business_a)
        await resolver.resolve(business_b)

    assert [item.phone_number_id for item in configurations] == [
        "physical-sender-a",
        "physical-sender-b",
    ]

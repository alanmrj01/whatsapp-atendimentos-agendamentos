from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Business, BusinessWhatsAppConnection
from app.whatsapp.administration import (
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.onboarding import (
    WhatsAppOnboardingError,
    WhatsAppOnboardingIntent,
    WhatsAppOnboardingService,
    WhatsAppProviderCompletion,
)
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
async def onboarding_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(delete(BusinessWhatsAppConnection))
        await session.execute(delete(Business))
    try:
        yield factory
    finally:
        await engine.dispose()


def completion(
    *,
    suffix: str,
    mode: WhatsAppConnectionMode = WhatsAppConnectionMode.COEXISTENCE,
    intent: WhatsAppOnboardingIntent = WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
) -> WhatsAppProviderCompletion:
    return WhatsAppProviderCompletion(
        intent=intent,
        confirmed_mode=mode,
        meta_waba_id=f"waba-{suffix}",
        meta_phone_number_id=f"phone-{suffix}",
        graph_version="v23.0",
        credential_secret_ref=(
            f"projects/project-{suffix}/secrets/whatsapp-token/versions/latest"
        ),
        provider_confirmed=True,
    )


async def add_business(
    factory: async_sessionmaker[AsyncSession],
    business_id: uuid.UUID,
) -> None:
    async with factory() as session, session.begin():
        session.add(Business(id=business_id, name="Onboarding physical test"))


async def test_concurrent_completion_cannot_overwrite_connected_configuration(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    await add_business(onboarding_sessions, business_id)
    async with onboarding_sessions() as setup, setup.begin():
        await WhatsAppConnectionAdministrationService(setup).create_pending_connection(
            business_id, WhatsAppConnectionMode.COEXISTENCE
        )

    second_result: list[Exception] = []
    async with onboarding_sessions() as first_session, onboarding_sessions() as second_session:
        async def second_completion() -> None:
            try:
                async with second_session.begin():
                    await WhatsAppOnboardingService(
                        WhatsAppConnectionAdministrationService(second_session)
                    ).complete_provider_onboarding(
                        business_id, completion(suffix="second")
                    )
            except Exception as exc:  # Captured for exact post-lock assertion.
                second_result.append(exc)

        async with first_session.begin():
            first = await WhatsAppOnboardingService(
                WhatsAppConnectionAdministrationService(first_session)
            ).complete_provider_onboarding(
                business_id, completion(suffix="first")
            )
            assert first.status is WhatsAppConnectionStatus.CONNECTED
            task = asyncio.create_task(second_completion())
            await asyncio.sleep(0.1)
            assert not task.done()  # The initial state read is protected by FOR UPDATE.
        await asyncio.wait_for(task, timeout=3)

    assert len(second_result) == 1
    assert isinstance(second_result[0], WhatsAppOnboardingError)
    async with onboarding_sessions() as session:
        connection = await session.scalar(
            select(BusinessWhatsAppConnection).where(
                BusinessWhatsAppConnection.business_id == business_id
            )
        )
        assert connection is not None
        assert connection.status == "connected"
        assert connection.meta_waba_id == "waba-first"
        assert connection.meta_phone_number_id == "phone-first"
        assert connection.credential_secret_ref == (
            "projects/project-first/secrets/whatsapp-token/versions/latest"
        )


async def test_connected_requires_all_provider_configuration(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    await add_business(onboarding_sessions, business_id)
    async with onboarding_sessions() as session, session.begin():
        administration = WhatsAppConnectionAdministrationService(session)
        await administration.create_pending_connection(
            business_id, WhatsAppConnectionMode.API_ONLY
        )
        await administration.update_meta_identifiers(
            business_id,
            meta_waba_id="waba-complete",
            meta_phone_number_id="phone-complete",
            graph_version="v23.0",
        )
        with pytest.raises(WhatsAppConnectionAdministrationError):
            await administration.mark_connected(business_id)
        pending = await administration.get_connection(business_id)
        assert pending is not None
        assert pending.status is WhatsAppConnectionStatus.PENDING


async def test_disconnected_restarts_without_cross_business_mutation(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_a, business_b = uuid.uuid4(), uuid.uuid4()
    await add_business(onboarding_sessions, business_a)
    await add_business(onboarding_sessions, business_b)
    async with onboarding_sessions() as session, session.begin():
        service = WhatsAppOnboardingService(
            WhatsAppConnectionAdministrationService(session)
        )
        first = await service.complete_provider_onboarding(
            business_a, completion(suffix="a")
        )
        disconnected = await service.disconnect(business_a)
        repeated = await service.disconnect(business_a)
        assert first.status is WhatsAppConnectionStatus.CONNECTED
        assert disconnected.status is WhatsAppConnectionStatus.DISCONNECTED
        assert repeated.id == disconnected.id
        restarted = await service.complete_provider_onboarding(
            business_a, completion(
                suffix="a-new",
                mode=WhatsAppConnectionMode.API_ONLY,
                intent=WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER,
            )
        )
        assert restarted.status is WhatsAppConnectionStatus.CONNECTED
        assert restarted.mode is WhatsAppConnectionMode.API_ONLY
        assert await WhatsAppConnectionAdministrationService(session).get_connection(
            business_b
        ) is None

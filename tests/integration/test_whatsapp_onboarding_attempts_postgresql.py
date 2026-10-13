from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Business,
    BusinessWhatsAppConnection,
    WhatsAppOnboardingAttempt,
)
from app.whatsapp.administration import (
    META_AUTHORIZATION_INCOMPLETE,
    META_ONBOARDING_PENDING,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.onboarding_attempts import WhatsAppOnboardingAttemptService
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
async def onboarding_sessions() -> AsyncIterator[
    async_sessionmaker[AsyncSession]
]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await session.execute(delete(WhatsAppOnboardingAttempt))
            await session.execute(delete(BusinessWhatsAppConnection))
            await session.execute(delete(Business))
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(delete(WhatsAppOnboardingAttempt))
            await session.execute(delete(BusinessWhatsAppConnection))
            await session.execute(delete(Business))
    await engine.dispose()


async def _create_pending(
    factory: async_sessionmaker[AsyncSession],
    business_id: uuid.UUID,
) -> uuid.UUID:
    async with factory() as session:
        async with session.begin():
            session.add(Business(id=business_id, name=f"Onboarding {business_id}"))
            await session.flush()
            connection = BusinessWhatsAppConnection(
                business_id=business_id,
                mode=WhatsAppConnectionMode.COEXISTENCE.value,
                status=WhatsAppConnectionStatus.PENDING.value,
                last_error_code=META_ONBOARDING_PENDING,
            )
            session.add(connection)
            await session.flush()
            return connection.id


async def test_different_businesses_can_create_attempts_concurrently(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_a = uuid.uuid4()
    business_b = uuid.uuid4()
    connection_a = await _create_pending(onboarding_sessions, business_a)
    connection_b = await _create_pending(onboarding_sessions, business_b)

    async def begin(
        business_id: uuid.UUID,
        connection_id: uuid.UUID,
    ):
        async with onboarding_sessions() as session:
            async with session.begin():
                service = WhatsAppOnboardingAttemptService(session)
                return await service.begin(
                    business_id,
                    connection_id,
                    WhatsAppConnectionMode.COEXISTENCE,
                    request_id=uuid.uuid4(),
                )

    attempt_a, attempt_b = await asyncio.gather(
        begin(business_a, connection_a),
        begin(business_b, connection_b),
    )

    assert attempt_a.business_id == business_a
    assert attempt_b.business_id == business_b
    assert attempt_a.id != attempt_b.id
    assert attempt_a.state == "created"
    assert attempt_b.state == "created"


async def test_same_business_reuses_single_active_attempt(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    connection_id = await _create_pending(onboarding_sessions, business_id)

    async with onboarding_sessions() as session:
        async with session.begin():
            service = WhatsAppOnboardingAttemptService(session)
            first = await service.begin(
                business_id,
                connection_id,
                WhatsAppConnectionMode.COEXISTENCE,
                request_id=uuid.uuid4(),
            )
            second = await service.begin(
                business_id,
                connection_id,
                WhatsAppConnectionMode.COEXISTENCE,
                request_id=uuid.uuid4(),
            )

    assert first.id == second.id


async def test_empty_callback_uses_grace_then_becomes_connection_error(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    connection_id = await _create_pending(onboarding_sessions, business_id)

    async with onboarding_sessions() as session:
        async with session.begin():
            attempts = WhatsAppOnboardingAttemptService(session)
            administration = WhatsAppConnectionAdministrationService(session)
            attempt = await attempts.begin(
                business_id,
                connection_id,
                WhatsAppConnectionMode.COEXISTENCE,
                request_id=uuid.uuid4(),
            )
            observed = await attempts.record_event(
                business_id,
                attempt.id,
                stage="login_callback_received",
                authorization_code_received=False,
            )
            assert observed.state == "awaiting_provider"

            model = await session.get(WhatsAppOnboardingAttempt, attempt.id)
            assert model is not None
            model.empty_callback_at = (
                datetime.now(timezone.utc) - timedelta(seconds=6)
            )
            await session.flush()

            terminal = await attempts.reconcile(business_id)
            assert terminal is not None
            connection = await administration.mark_authorization_incomplete(
                business_id,
            )

    assert terminal.state == "authorization_incomplete"
    assert terminal.failure_code == META_AUTHORIZATION_INCOMPLETE
    assert connection.status is WhatsAppConnectionStatus.ERROR
    assert connection.last_error_code == META_AUTHORIZATION_INCOMPLETE


async def test_expired_attempt_is_reconciled_server_side(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    connection_id = await _create_pending(onboarding_sessions, business_id)

    async with onboarding_sessions() as session:
        async with session.begin():
            attempt = WhatsAppOnboardingAttempt(
                business_id=business_id,
                connection_id=connection_id,
                request_id=uuid.uuid4(),
                mode=WhatsAppConnectionMode.COEXISTENCE.value,
                state="awaiting_provider",
                last_event_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            session.add(attempt)
            await session.flush()

            service = WhatsAppOnboardingAttemptService(session)
            expired = await service.reconcile(business_id)

    assert expired is not None
    assert expired.state == "expired"
    assert expired.failure_code == "META_ONBOARDING_EXPIRED"


async def test_old_terminal_attempt_cannot_become_active_again(
    onboarding_sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    connection_id = await _create_pending(onboarding_sessions, business_id)

    async with onboarding_sessions() as session:
        async with session.begin():
            service = WhatsAppOnboardingAttemptService(session)
            first = await service.begin(
                business_id,
                connection_id,
                WhatsAppConnectionMode.COEXISTENCE,
                request_id=uuid.uuid4(),
            )
            await service.record_event(
                business_id,
                first.id,
                stage="login_callback_received",
                authorization_code_received=False,
            )

            second = await service.begin(
                business_id,
                connection_id,
                WhatsAppConnectionMode.COEXISTENCE,
                request_id=uuid.uuid4(),
            )
            stale = await service.record_event(
                business_id,
                first.id,
                stage="login_callback_received",
                authorization_code_received=False,
            )

    assert first.id != second.id
    assert second.state == "created"
    assert stale.id == first.id
    assert stale.state == "authorization_incomplete"

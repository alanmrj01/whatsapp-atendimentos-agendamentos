from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.billing.service import BillingService
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


class _GatewayMustNotBeCalled:
    def __getattr__(self, name):
        raise AssertionError(f"Provider gateway must not be called: {name}")


async def test_billing_advisory_lock_serializes_same_business(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_PROVIDER_ENVIRONMENT", "sandbox")
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    business_id = uuid4()

    try:
        async with sessions() as first, sessions() as second:
            first_service = BillingService(first, _GatewayMustNotBeCalled())
            second_service = BillingService(second, _GatewayMustNotBeCalled())
            await first_service._lock_business_checkout(business_id)

            waiter = asyncio.create_task(
                second_service._lock_business_checkout(business_id)
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), timeout=0.2)

            await first.commit()
            await asyncio.wait_for(waiter, timeout=2)
            await second.rollback()
    finally:
        await engine.dispose()

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Business, BusinessUserMembership, ReengagementDelivery, User
from app.reengagement.service import ReengagementService
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


async def _seed(factory):
    business_id = uuid4()
    user_id = uuid4()
    created_at = datetime.now(UTC) - timedelta(days=2)
    async with factory() as db:
        async with db.begin():
            db.add(Business(id=business_id, name="Reengagement concurrency"))
            db.add(
                User(
                    id=user_id,
                    email=f"{user_id}@example.test",
                    password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$YWJj",
                    is_active=True,
                    created_at=created_at,
                )
            )
            await db.flush()
            db.add(
                BusinessUserMembership(
                    user_id=user_id,
                    business_id=business_id,
                    role="owner",
                )
            )
    return business_id, user_id


async def test_due_delivery_insert_is_idempotent_across_concurrent_sessions():
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id, user_id = await _seed(factory)
    membership = SimpleNamespace(
        business_id=business_id,
        role="owner",
        access_mode="free",
    )

    async def claim():
        async with factory() as db:
            prompt = await ReengagementService(db).claim_prompt(
                user_id=user_id,
                membership=membership,
            )
            return prompt

    try:
        results = await asyncio.gather(claim(), claim())
        assert sum(item is not None for item in results) == 1

        async with factory() as db:
            count = await db.scalar(
                select(func.count(ReengagementDelivery.id)).where(
                    ReengagementDelivery.user_id == user_id,
                    ReengagementDelivery.business_id == business_id,
                    ReengagementDelivery.campaign == "upgrade",
                    ReengagementDelivery.step == 1,
                )
            )
            assert count == 1
    finally:
        await engine.dispose()


async def test_email_delivery_claim_is_atomic_across_workers():
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id, user_id = await _seed(factory)
    delivery_id = uuid4()

    async with factory() as db:
        async with db.begin():
            db.add(
                ReengagementDelivery(
                    id=delivery_id,
                    user_id=user_id,
                    business_id=business_id,
                    campaign="upgrade",
                    step=1,
                )
            )

    async def claim():
        async with factory() as db:
            return await ReengagementService(db)._claim_email_delivery(delivery_id)

    try:
        results = await asyncio.gather(claim(), claim())
        assert sorted(result is not None for result in results) == [False, True]

        async with factory() as db:
            row = await db.get(ReengagementDelivery, delivery_id)
            assert row is not None
            assert row.email_claimed_at is not None
            assert row.email_sent_at is None
            assert row.email_failed_at is None
    finally:
        await engine.dispose()

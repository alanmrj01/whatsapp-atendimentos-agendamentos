from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    AuthSession,
    Business,
    BusinessUserMembership,
    User,
    WebPushDelivery,
    WebPushEvent,
    WebPushSubscription,
)
from app.repositories.web_push import WebPushRepository
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


async def test_web_push_physical_tenant_constraints_dedup_and_rls() -> None:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_a, business_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    session_a, session_b = uuid4(), uuid4()
    subscription_a, subscription_b = uuid4(), uuid4()
    endpoint_hash = "a" * 64

    try:
        async with factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        Business(id=business_a, name="Tenant A"),
                        Business(id=business_b, name="Tenant B"),
                        User(
                            id=user_a,
                            email=f"{user_a}@example.test",
                            password_hash="$argon2id$test",
                            is_active=True,
                        ),
                        User(
                            id=user_b,
                            email=f"{user_b}@example.test",
                            password_hash="$argon2id$test",
                            is_active=True,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        BusinessUserMembership(
                            user_id=user_a, business_id=business_a, role="owner"
                        ),
                        BusinessUserMembership(
                            user_id=user_b, business_id=business_b, role="owner"
                        ),
                        AuthSession(
                            id=session_a,
                            user_id=user_a,
                            active_business_id=business_a,
                            refresh_token_hash="1" * 64,
                            expires_at=datetime.now(UTC) + timedelta(hours=1),
                        ),
                        AuthSession(
                            id=session_b,
                            user_id=user_b,
                            active_business_id=business_b,
                            refresh_token_hash="2" * 64,
                            expires_at=datetime.now(UTC) + timedelta(hours=1),
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        WebPushSubscription(
                            id=subscription_a,
                            user_id=user_a,
                            business_id=business_a,
                            auth_session_id=session_a,
                            endpoint_hash=endpoint_hash,
                            endpoint="https://push.example.test/shared",
                            p256dh="p256dh-a",
                            auth_secret="auth-a",
                        ),
                        WebPushSubscription(
                            id=subscription_b,
                            user_id=user_b,
                            business_id=business_b,
                            auth_session_id=session_b,
                            endpoint_hash=endpoint_hash,
                            endpoint="https://push.example.test/shared",
                            p256dh="p256dh-b",
                            auth_secret="auth-b",
                        ),
                    ]
                )

        async with factory() as session:
            active_a = await WebPushRepository(session).active_subscriptions(
                business_a
            )
            assert [item.id for item in active_a] == [subscription_a]
            rls_tables = set(
                (
                    await session.scalars(
                        text(
                            "SELECT relname FROM pg_class "
                            "WHERE relname LIKE 'web_push_%' AND relrowsecurity"
                        )
                    )
                ).all()
            )
            assert rls_tables == {
                "web_push_subscriptions",
                "web_push_events",
                "web_push_deliveries",
            }

        async with factory() as session:
            session.add(
                WebPushSubscription(
                    id=uuid4(),
                    user_id=user_a,
                    business_id=business_b,
                    auth_session_id=session_a,
                    endpoint_hash="b" * 64,
                    endpoint="https://push.example.test/cross-tenant",
                    p256dh="p256dh-cross",
                    auth_secret="auth-cross",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        async with factory() as session:
            async with session.begin():
                for model in (
                    WebPushDelivery,
                    WebPushEvent,
                    WebPushSubscription,
                    AuthSession,
                    BusinessUserMembership,
                    User,
                    Business,
                ):
                    await session.execute(delete(model))
    finally:
        await engine.dispose()

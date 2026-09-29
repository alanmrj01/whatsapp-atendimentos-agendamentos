from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Business,
    Conversation,
    Customer,
    Message,
    ProcessedWebhook,
)
from app.tasks.worker import process_cloud_task_event
from app.whatsapp.webhook import build_event_key
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL,
    migrated_test_database,
    sessions,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
    ),
]

assert migrated_test_database
assert sessions


@pytest_asyncio.fixture(autouse=True)
async def cleanup_cloud_task_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    yield
    async with sessions() as session:
        async with session.begin():
            for model in (
                ProcessedWebhook,
                Message,
                Conversation,
                Customer,
                Business,
            ):
                await session.execute(delete(model))


async def test_concurrent_workers_fetch_db_event_and_create_one_outbox(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    provider_message_id = "provider-physical-cloud-task"
    event_key = build_event_key("inbound", provider_message_id)

    async with sessions() as session:
        async with session.begin():
            session.add(
                Business(
                    id=business_id,
                    name="Cloud Tasks physical test",
                )
            )
            await session.flush()
            session.add(
                Customer(
                    id=customer_id,
                    business_id=business_id,
                    whatsapp_id="5511999990099",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    id=conversation_id,
                    business_id=business_id,
                    customer_id=customer_id,
                    state="START",
                    context={},
                    automation_enabled=True,
                    handoff_status="none",
                    last_interaction_at=datetime.now(timezone.utc),
                )
            )
            await session.flush()
            session.add_all(
                [
                    Message(
                        id=uuid.uuid4(),
                        business_id=business_id,
                        conversation_id=conversation_id,
                        provider_message_id=provider_message_id,
                        direction="inbound",
                        message_type="text",
                        body="hello",
                        status="received",
                    ),
                    ProcessedWebhook(
                        id=uuid.uuid4(),
                        event_key=event_key,
                        provider_message_id=provider_message_id,
                        event_type="message.inbound.text",
                        status="queued",
                        attempts=1,
                    ),
                ]
            )

    async def run_worker() -> bool:
        async with sessions() as session:
            return await process_cloud_task_event(session, event_key)

    results = await asyncio.gather(run_worker(), run_worker())

    async with sessions() as session:
        processed = await session.scalar(
            select(ProcessedWebhook).where(
                ProcessedWebhook.event_key == event_key
            )
        )
        outbound_count = await session.scalar(
            select(func.count(Message.id)).where(Message.direction == "outbound")
        )
        conversation_state = await session.scalar(
            select(Conversation.state).where(Conversation.id == conversation_id)
        )

    assert sorted(results) == [False, True]
    assert processed is not None
    assert processed.status == "processed"
    assert processed.processed_at is not None
    assert processed.attempts == 2
    assert outbound_count == 1
    assert conversation_state == "MENU"


async def test_failed_delivery_status_reopens_outbound_for_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    provider_message_id = "wamid.failed-delivery"
    event_key = build_event_key("status", provider_message_id, "failed")

    async with sessions() as session:
        async with session.begin():
            session.add(Business(id=business_id, name="Delivery retry physical test"))
            await session.flush()
            session.add(
                Customer(
                    id=customer_id,
                    business_id=business_id,
                    whatsapp_id="5511999990098",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    id=conversation_id,
                    business_id=business_id,
                    customer_id=customer_id,
                    state="EQUIPMENT_DELIVERY",
                    context={},
                    automation_enabled=True,
                    handoff_status="none",
                    last_interaction_at=datetime.now(timezone.utc),
                )
            )
            await session.flush()
            session.add_all(
                [
                    Message(
                        id=message_id,
                        business_id=business_id,
                        conversation_id=conversation_id,
                        provider_message_id=provider_message_id,
                        direction="outbound",
                        message_type="text",
                        body="Como você prefere receber o aparelho?",
                        status="sent",
                        outbound_payload=None,
                    ),
                    ProcessedWebhook(
                        id=uuid.uuid4(),
                        event_key=event_key,
                        provider_message_id=provider_message_id,
                        event_type="message.status.failed",
                        status="queued",
                        attempts=1,
                    ),
                ]
            )

    async with sessions() as session:
        assert await process_cloud_task_event(session, event_key) is True

    async with sessions() as session:
        message = await session.scalar(
            select(Message).where(Message.id == message_id)
        )
        event = await session.scalar(
            select(ProcessedWebhook).where(
                ProcessedWebhook.event_key == event_key
            )
        )

    assert message is not None
    assert message.status == "pending"
    assert message.provider_message_id == provider_message_id
    assert message.outbound_payload is not None
    assert message.outbound_payload["_alovia_retry_attempt"] == 1
    assert event is not None
    assert event.status == "processed"

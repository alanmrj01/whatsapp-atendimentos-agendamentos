from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Business,
    Conversation,
    Customer,
    Message,
    ProcessedWebhook,
)
from app.tasks.outbound import (
    OutboundTaskTransientError,
    enqueue_pending_outbounds_for_event,
    process_outbound_message,
)
from app.whatsapp.client import WhatsAppPermanentError, WhatsAppTimeoutError
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
async def cleanup_outbound_task_rows(
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


class CountingSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def send_text(self, _: str, __: str) -> str:
        self.calls += 1
        await asyncio.sleep(0.05)
        if self.error is not None:
            raise self.error
        return "wamid.physical-outbound"

    async def send_interactive_buttons(
        self, _: str, __: str, ___: Any
    ) -> str:
        raise AssertionError("unexpected buttons send")

    async def send_interactive_list(
        self, _: str, __: str, ___: Any
    ) -> str:
        raise AssertionError("unexpected list send")

    async def aclose(self) -> None:
        return None


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.message_ids: list[uuid.UUID] = []

    async def enqueue(self, message_id: uuid.UUID) -> None:
        self.message_ids.append(message_id)


async def seed_pending_outbound(
    sessions: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[uuid.UUID, str]:
    business_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    inbound_id = uuid.uuid4()
    outbound_id = uuid.uuid4()
    provider_message_id = f"provider-outbound-{suffix}"
    event_key = build_event_key("inbound", provider_message_id)

    async with sessions() as session:
        async with session.begin():
            session.add(Business(id=business_id, name="Outbound physical test"))
            await session.flush()
            session.add(
                Customer(
                    id=customer_id,
                    business_id=business_id,
                    whatsapp_id="5511999990088",
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    id=conversation_id,
                    business_id=business_id,
                    customer_id=customer_id,
                    state="MENU",
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
                        id=inbound_id,
                        business_id=business_id,
                        conversation_id=conversation_id,
                        provider_message_id=provider_message_id,
                        direction="inbound",
                        message_type="text",
                        body="inbound",
                        status="received",
                    ),
                    Message(
                        id=outbound_id,
                        business_id=business_id,
                        conversation_id=conversation_id,
                        direction="outbound",
                        message_type="text",
                        body="outbound",
                        status="pending",
                        idempotency_key=f"physical-outbound-{suffix}",
                    ),
                    ProcessedWebhook(
                        id=uuid.uuid4(),
                        event_key=event_key,
                        provider_message_id=provider_message_id,
                        event_type="message.inbound.text",
                        status="processed",
                        attempts=1,
                    ),
                ]
            )
    return outbound_id, event_key


async def test_pending_discovery_and_concurrent_workers_send_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    message_id, event_key = await seed_pending_outbound(
        sessions,
        suffix="concurrent",
    )
    enqueuer = RecordingEnqueuer()
    async with sessions() as session:
        discovered = await enqueue_pending_outbounds_for_event(
            session,
            event_key,
            enqueuer,
        )

    sender = CountingSender()

    async def run_worker() -> str:
        async with sessions() as session:
            return await process_outbound_message(
                session,
                message_id,
                lambda: sender,
            )

    results = await asyncio.gather(run_worker(), run_worker())

    async with sessions() as session:
        message = await session.scalar(
            select(Message).where(Message.id == message_id)
        )

    assert discovered == [message_id]
    assert enqueuer.message_ids == [message_id]
    assert sorted(results) == ["sent", "skipped"]
    assert sender.calls == 1
    assert message is not None
    assert message.status == "sent"
    assert message.provider_message_id == "wamid.physical-outbound"


async def test_transient_failure_stays_pending_then_success_is_terminal(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    message_id, _ = await seed_pending_outbound(sessions, suffix="retry")
    transient_sender = CountingSender(WhatsAppTimeoutError("timeout"))

    async with sessions() as session:
        with pytest.raises(OutboundTaskTransientError):
            await process_outbound_message(
                session,
                message_id,
                lambda: transient_sender,
            )

    async with sessions() as session:
        after_failure = await session.scalar(
            select(Message).where(Message.id == message_id)
        )
    assert after_failure is not None
    assert after_failure.status == "pending"
    assert after_failure.provider_message_id is None

    success_sender = CountingSender()
    async with sessions() as session:
        assert await process_outbound_message(
            session,
            message_id,
            lambda: success_sender,
        ) == "sent"
    async with sessions() as session:
        assert await process_outbound_message(
            session,
            message_id,
            lambda: success_sender,
        ) == "skipped"

    async with sessions() as session:
        after_success = await session.scalar(
            select(Message).where(Message.id == message_id)
        )
    assert after_success is not None
    assert after_success.status == "sent"
    assert after_success.provider_message_id == "wamid.physical-outbound"
    assert success_sender.calls == 1


async def test_permanent_failure_is_persisted_and_never_retried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    message_id, _ = await seed_pending_outbound(sessions, suffix="permanent")
    sender = CountingSender(WhatsAppPermanentError("rejected"))

    async with sessions() as session:
        assert await process_outbound_message(
            session,
            message_id,
            lambda: sender,
        ) == "failed"
    async with sessions() as session:
        assert await process_outbound_message(
            session,
            message_id,
            lambda: sender,
        ) == "skipped"

    async with sessions() as session:
        message = await session.scalar(
            select(Message).where(Message.id == message_id)
        )
    assert message is not None
    assert message.status == "failed"
    assert message.provider_message_id is None
    assert sender.calls == 1

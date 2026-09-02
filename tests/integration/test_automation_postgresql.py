from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Business,
    BusinessAutomationExclusion,
    Conversation,
    Customer,
    Message,
    ProcessedWebhook,
)
from app.tasks.worker import process_cloud_task_event
from app.tasks.outbound import process_outbound_message
from app.whatsapp.processor import (
    persist_webhook_events_for_tasks,
    process_webhook_events,
)
from app.whatsapp.webhook import (
    BusinessMessageEchoEvent,
    InboundMessageEvent,
    build_event_key,
)
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
async def cleanup_automation_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    yield
    async with sessions() as session:
        async with session.begin():
            for model in (
                ProcessedWebhook,
                Message,
                BusinessAutomationExclusion,
                Conversation,
                Customer,
                Business,
            ):
                await session.execute(delete(model))


def inbound(
    message_id: str,
    whatsapp_id: str,
    phone_number_id: str,
) -> InboundMessageEvent:
    return InboundMessageEvent(
        event_key=build_event_key("inbound", message_id),
        event_type="message.inbound.text",
        meta_phone_number_id=phone_number_id,
        provider_message_id=message_id,
        whatsapp_id=whatsapp_id,
        message_type="text",
        body="private inbound",
        interactive_id=None,
    )


def manual_echo(
    message_id: str,
    whatsapp_id: str,
    phone_number_id: str,
    occurred_at: datetime,
) -> BusinessMessageEchoEvent:
    return BusinessMessageEchoEvent(
        event_key=build_event_key("business_echo", message_id),
        event_type="message.business_echo.text",
        meta_phone_number_id=phone_number_id,
        provider_message_id=message_id,
        whatsapp_id=whatsapp_id,
        message_type="text",
        occurred_at=occurred_at,
    )


async def add_business(
    sessions: async_sessionmaker[AsyncSession],
    *,
    phone_number_id: str,
    window: int = 2160,
) -> uuid.UUID:
    business_id = uuid.uuid4()
    async with sessions() as session:
        async with session.begin():
            session.add(
                Business(
                    id=business_id,
                    name="Automation physical test",
                    meta_phone_number_id=phone_number_id,
                    human_control_window_minutes=window,
                )
            )
    return business_id


async def test_exclusion_unique_scope_and_database_checks(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first_business = await add_business(
        sessions, phone_number_id=f"physical-{uuid.uuid4()}"
    )
    second_business = await add_business(
        sessions, phone_number_id=f"physical-{uuid.uuid4()}"
    )
    whatsapp_id = "5511999991010"

    async with sessions() as session:
        async with session.begin():
            session.add_all(
                [
                    BusinessAutomationExclusion(
                        business_id=first_business,
                        whatsapp_id=whatsapp_id,
                        mode="ignore",
                    ),
                    BusinessAutomationExclusion(
                        business_id=second_business,
                        whatsapp_id=whatsapp_id,
                        mode="human_only",
                    ),
                ]
            )

    async with sessions() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    BusinessAutomationExclusion(
                        business_id=first_business,
                        whatsapp_id=whatsapp_id,
                        mode="human_only",
                    )
                )
                await session.flush()

    for invalid_values in (
        {"whatsapp_id": "group@g.us", "mode": "ignore"},
        {"whatsapp_id": "5511999992020", "mode": "unsupported"},
    ):
        async with sessions() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    session.add(
                        BusinessAutomationExclusion(
                            business_id=first_business,
                            **invalid_values,
                        )
                    )
                    await session.flush()

    async with sessions() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(
                    Business(
                        name="Invalid free-form window",
                        human_control_window_minutes=31,
                    )
                )
                await session.flush()


async def test_ignore_human_only_deactivation_and_multitenancy(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ignored_business = await add_business(
        sessions, phone_number_id="physical-ignore-phone"
    )
    human_business = await add_business(
        sessions, phone_number_id="physical-human-phone"
    )
    whatsapp_id = "5511999993030"
    async with sessions() as session:
        async with session.begin():
            session.add_all(
                [
                    BusinessAutomationExclusion(
                        business_id=ignored_business,
                        whatsapp_id=whatsapp_id,
                        mode="ignore",
                    ),
                    BusinessAutomationExclusion(
                        business_id=human_business,
                        whatsapp_id=whatsapp_id,
                        mode="human_only",
                    ),
                ]
            )

    ignored = inbound("physical-ignore-event", whatsapp_id, "physical-ignore-phone")
    human = inbound("physical-human-event", whatsapp_id, "physical-human-phone")
    async with sessions() as session:
        await process_webhook_events(session, [ignored, human])

    async with sessions() as session:
        ignored_customer_count = await session.scalar(
            select(func.count(Customer.id)).where(
                Customer.business_id == ignored_business
            )
        )
        human_conversation = await session.scalar(
            select(Conversation).where(
                Conversation.business_id == human_business
            )
        )
        human_outbound_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == human_business,
                Message.direction == "outbound",
            )
        )
    assert ignored_customer_count == 0
    assert human_conversation is not None
    assert human_conversation.handoff_status == "human_only"
    assert human_outbound_count == 0

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                update(BusinessAutomationExclusion)
                .where(
                    BusinessAutomationExclusion.business_id == human_business
                )
                .values(active=False)
            )
    resumed = inbound(
        "physical-human-resumed",
        whatsapp_id,
        "physical-human-phone",
    )
    async with sessions() as session:
        await process_webhook_events(session, [resumed])
    async with sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.business_id == human_business
            )
        )
        outbound_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == human_business,
                Message.direction == "outbound",
                Message.status == "pending",
            )
        )
    assert conversation is not None
    assert conversation.handoff_status == "none"
    assert outbound_count == 1


async def test_manual_echo_suppresses_renews_and_resumes_without_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await add_business(
        sessions,
        phone_number_id="physical-coexistence-phone",
        window=30,
    )
    whatsapp_id = "5511999994040"
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    first_echo = manual_echo(
        "physical-manual-1",
        whatsapp_id,
        "physical-coexistence-phone",
        started_at,
    )
    renewed_at = started_at + timedelta(minutes=5)
    second_echo = manual_echo(
        "physical-manual-2",
        whatsapp_id,
        "physical-coexistence-phone",
        renewed_at,
    )

    async with sessions() as session:
        await process_webhook_events(session, [first_echo, second_echo])

    async with sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.business_id == business_id)
        )
        exclusion_count = await session.scalar(
            select(func.count(BusinessAutomationExclusion.id)).where(
                BusinessAutomationExclusion.business_id == business_id
            )
        )
        message_count = await session.scalar(
            select(func.count(Message.id)).where(Message.business_id == business_id)
        )
    assert conversation is not None
    assert conversation.conversation_initiated_by == "business"
    assert conversation.human_control_started_at == started_at
    assert conversation.last_human_message_at == renewed_at
    assert conversation.automation_suppressed_until == renewed_at + timedelta(
        minutes=30
    )
    assert exclusion_count == 0
    assert message_count == 0

    during = inbound(
        "physical-customer-during",
        whatsapp_id,
        "physical-coexistence-phone",
    )
    async with sessions() as session:
        await process_webhook_events(session, [during])
    async with sessions() as session:
        outbound_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
            )
        )
        during_status = await session.scalar(
            select(ProcessedWebhook.status).where(
                ProcessedWebhook.event_key == during.event_key
            )
        )
    assert outbound_count == 0
    assert during_status == "ignored"

    async with sessions() as session:
        async with session.begin():
            await session.execute(
                update(Conversation)
                .where(Conversation.business_id == business_id)
                .values(
                    automation_suppressed_until=datetime.now(timezone.utc)
                    - timedelta(seconds=1)
                )
            )
    after = inbound(
        "physical-customer-after",
        whatsapp_id,
        "physical-coexistence-phone",
    )
    async with sessions() as session:
        await process_webhook_events(session, [after])
    async with sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.business_id == business_id)
        )
        pending_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status == "pending",
            )
        )
    assert conversation is not None
    assert conversation.automation_suppressed_until is None
    assert conversation.suppression_reason is None
    assert conversation.conversation_initiated_by == "business"
    assert pending_count == 1


async def test_delayed_older_manual_echo_never_shortens_newer_window(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await add_business(
        sessions,
        phone_number_id="physical-out-of-order-phone",
        window=30,
    )
    whatsapp_id = "5511999994141"
    first_at = datetime.now(timezone.utc).replace(microsecond=0)
    latest_at = first_at + timedelta(minutes=10)
    delayed_at = first_at + timedelta(minutes=5)

    async with sessions() as session:
        await process_webhook_events(
            session,
            [
                manual_echo(
                    "physical-manual-first",
                    whatsapp_id,
                    "physical-out-of-order-phone",
                    first_at,
                ),
                manual_echo(
                    "physical-manual-latest",
                    whatsapp_id,
                    "physical-out-of-order-phone",
                    latest_at,
                ),
                manual_echo(
                    "physical-manual-delayed",
                    whatsapp_id,
                    "physical-out-of-order-phone",
                    delayed_at,
                ),
            ],
        )

    async with sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.business_id == business_id)
        )
    assert conversation is not None
    assert conversation.human_control_started_at == first_at
    assert conversation.last_human_message_at == latest_at
    assert conversation.automation_suppressed_until == latest_at + timedelta(
        minutes=30
    )


async def test_customer_initiation_is_preserved_when_human_later_takes_control(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await add_business(
        sessions, phone_number_id="physical-customer-first-phone"
    )
    whatsapp_id = "5511999995050"
    customer_event = inbound(
        "physical-customer-first",
        whatsapp_id,
        "physical-customer-first-phone",
    )
    human_event = manual_echo(
        "physical-human-after-customer",
        whatsapp_id,
        "physical-customer-first-phone",
        datetime.now(timezone.utc),
    )

    async with sessions() as session:
        await process_webhook_events(session, [customer_event, human_event])
    async with sessions() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.business_id == business_id)
        )
        pending_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status == "pending",
            )
        )
        failed_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status == "failed",
            )
        )
    assert conversation is not None
    assert conversation.conversation_initiated_by == "customer"
    assert conversation.automation_suppressed_until is not None
    assert pending_count == 0
    assert failed_count == 1


async def test_cloud_task_worker_rechecks_exclusion_before_engine(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await add_business(
        sessions, phone_number_id="physical-queued-policy-phone"
    )
    whatsapp_id = "5511999996060"
    event = inbound(
        "physical-queued-policy",
        whatsapp_id,
        "physical-queued-policy-phone",
    )
    async with sessions() as session:
        event_keys = await persist_webhook_events_for_tasks(session, [event])
    assert event_keys == [event.event_key]

    async with sessions() as session:
        async with session.begin():
            session.add(
                BusinessAutomationExclusion(
                    business_id=business_id,
                    whatsapp_id=whatsapp_id,
                    mode="ignore",
                )
            )
    async with sessions() as session:
        assert await process_cloud_task_event(session, event.event_key) is False
    async with sessions() as session:
        event_status = await session.scalar(
            select(ProcessedWebhook.status).where(
                ProcessedWebhook.event_key == event.event_key
            )
        )
        outbound_count = await session.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
            )
        )
    assert event_status == "ignored"
    assert outbound_count == 0


async def test_outbound_sender_rechecks_active_exclusion_before_meta_call(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await add_business(
        sessions, phone_number_id="physical-outbound-policy-phone"
    )
    whatsapp_id = "5511999997070"
    event = inbound(
        "physical-outbound-policy",
        whatsapp_id,
        "physical-outbound-policy-phone",
    )
    async with sessions() as session:
        await process_webhook_events(session, [event])
    async with sessions() as session:
        outbound_id = await session.scalar(
            select(Message.id).where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status == "pending",
            )
        )
    assert outbound_id is not None

    async with sessions() as session:
        async with session.begin():
            session.add(
                BusinessAutomationExclusion(
                    business_id=business_id,
                    whatsapp_id=whatsapp_id,
                    mode="ignore",
                )
            )

    def external_call_must_not_start():  # type: ignore[no-untyped-def]
        raise AssertionError("Meta call must not start for an exclusion")

    async with sessions() as session:
        result = await process_outbound_message(
            session,
            outbound_id,
            external_call_must_not_start,
        )
    async with sessions() as session:
        status = await session.scalar(
            select(Message.status).where(Message.id == outbound_id)
        )
    assert result == "failed"
    assert status == "failed"

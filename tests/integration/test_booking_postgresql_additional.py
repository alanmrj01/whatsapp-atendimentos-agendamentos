from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automation.lifecycle import create_due_lifecycle_outreach, mark_outreach_response
from app.booking.availability import PostgresBookingAvailabilityPort
from app.conversations.ports import SlotUnavailable
from app.models import Appointment, Conversation, CustomerOutreach, Message, ScheduleBlock
from app.repositories.outbound_tasks import OutboundTaskRepository
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL,
    _physical_appointment,
    confirm_in_new_transaction,
    migrated_test_database,
    requirements,
    seed_capacity,
    sessions,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
    ),
]

# Fixtures importadas explicitamente para preservar o mesmo ciclo upgrade/downgrade
# e a mesma proteção de banco descartável da suíte A-J.
assert migrated_test_database
assert sessions


async def test_physical_schema_is_at_head_with_immutable_exclude_support(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        volatility = await session.scalar(
            text(
                "SELECT provolatile::text FROM pg_proc "
                "WHERE proname = 'booking_add_minutes_immutable'"
            )
        )
        constraint = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = "
                "'excl_appointments_employee_confirmed_overlap'"
            )
        )

    assert revision == "20261001_0026"
    assert volatility == "i"
    assert constraint is not None
    assert "tstzrange" in constraint
    assert "booking_add_minutes_immutable" in constraint


async def test_exclude_enforces_travel_boundary_and_one_minute_overlap(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, employees, customers, selected_date = (
        await seed_capacity(sessions, customer_count=3)
    )
    local_start = datetime.combine(
        datetime.fromisoformat(selected_date).date(),
        time(14),
        ZoneInfo("America/Sao_Paulo"),
    ).astimezone(timezone.utc)
    first = _physical_appointment(
        business_id,
        customers[0],
        service_id,
        employees[0],
        local_start,
        "additional:exclude:first",
        travel_before_minutes=30,
        travel_after_minutes=30,
    )
    async with sessions() as session:
        session.add(first)
        await session.commit()

    for offset, key in (
        (timedelta(hours=1, minutes=15), "additional:exclude:15min"),
        (timedelta(hours=1, minutes=29), "additional:exclude:1min"),
    ):
        overlapping = _physical_appointment(
            business_id,
            customers[1],
            service_id,
            employees[0],
            local_start + offset,
            key,
        )
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                session.add(overlapping)
                await session.commit()

    boundary = _physical_appointment(
        business_id,
        customers[2],
        service_id,
        employees[0],
        local_start + timedelta(hours=1, minutes=30),
        "additional:exclude:boundary",
    )
    async with sessions() as session:
        session.add(boundary)
        await session.commit()
        count = await session.scalar(select(func.count(Appointment.id)))

    assert count == 2


async def test_schedule_block_prevents_physical_booking(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, employees, customers, selected_date = (
        await seed_capacity(sessions)
    )
    block_start = datetime.combine(
        datetime.fromisoformat(selected_date).date(),
        time(10),
        ZoneInfo("America/Sao_Paulo"),
    ).astimezone(timezone.utc)
    async with sessions() as session:
        session.add(
            ScheduleBlock(
                business_id=business_id,
                employee_id=employees[0],
                starts_at=block_start,
                ends_at=block_start + timedelta(hours=1),
                reason="Teste físico",
            )
        )
        await session.commit()

    async with sessions() as session:
        options = await PostgresBookingAvailabilityPort(session).list_times(
            business_id,
            service_id,
            selected_date,
            requirements("additional:block:list"),
        )
    assert "10:00" not in {option.id for option in options}

    with pytest.raises(SlotUnavailable):
        await confirm_in_new_transaction(
            sessions,
            business_id,
            customers[0],
            service_id,
            selected_date,
            "10:00",
            "additional:block:confirm",
        )


async def test_composite_foreign_keys_reject_cross_business_appointments(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_a, service_a, employees_a, customers_a, selected_date = (
        await seed_capacity(sessions)
    )
    _, service_b, employees_b, customers_b, _ = await seed_capacity(sessions)
    starts_at = datetime.combine(
        datetime.fromisoformat(selected_date).date(),
        time(10),
        ZoneInfo("America/Sao_Paulo"),
    ).astimezone(timezone.utc)

    mismatches = (
        (customers_b[0], service_a, employees_a[0], "customer"),
        (customers_a[0], service_b, employees_a[0], "service"),
        (customers_a[0], service_a, employees_b[0], "employee"),
    )
    for customer_id, service_id, employee_id, suffix in mismatches:
        appointment = _physical_appointment(
            business_a,
            customer_id,
            service_id,
            employee_id,
            starts_at,
            f"additional:cross-business:{suffix}",
        )
        with pytest.raises(IntegrityError):
            async with sessions() as session:
                session.add(appointment)
                await session.commit()

    async with sessions() as session:
        count = await session.scalar(select(func.count(Appointment.id)))
    assert count == 0


async def test_preventive_outreach_can_be_fast_forwarded_without_waiting_six_months(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, employees, customers, _ = await seed_capacity(
        sessions,
        customer_count=1,
    )
    reference = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    starts_at = reference - timedelta(days=181, hours=1)
    completed = _physical_appointment(
        business_id,
        customers[0],
        service_id,
        employees[0],
        starts_at,
        "additional:preventive:completed",
    )
    completed.status = "completed"

    async with sessions() as session:
        async with session.begin():
            session.add(completed)
            session.add(
                Conversation(
                    business_id=business_id,
                    customer_id=customers[0],
                    state="COMPLETED",
                    context={},
                    automation_enabled=True,
                    handoff_status="none",
                    last_interaction_at=completed.ends_at,
                )
            )

    async with sessions() as session:
        async with session.begin():
            created = await create_due_lifecycle_outreach(
                session,
                now=reference,
                limit=100,
            )
    assert len(created) == 1

    async with sessions() as session:
        outreach = await session.scalar(
            select(CustomerOutreach).where(
                CustomerOutreach.business_id == business_id,
                CustomerOutreach.customer_id == customers[0],
                CustomerOutreach.outreach_type == "cleaning_6m",
            )
        )
        messages = (
            await session.scalars(
                select(Message).where(
                    Message.business_id == business_id,
                    Message.conversation_id == outreach.conversation_id,
                    Message.direction == "outbound",
                )
            )
        ).all()

    assert outreach is not None
    assert outreach.status == "pending"
    assert outreach.source_appointment_id == completed.id
    assert outreach.due_at == completed.ends_at + timedelta(days=180)
    assert len(messages) == 2
    assert {message.outbound_payload.get("_alovia_sequence_index") for message in messages} == {0, 1}
    assert all(message.outbound_payload.get("_alovia_outreach_id") == str(outreach.id) for message in messages)

    async with sessions() as session:
        async with session.begin():
            duplicate = await create_due_lifecycle_outreach(
                session,
                now=reference,
                limit=100,
            )
    assert duplicate == []

    ordered = sorted(
        messages,
        key=lambda message: int(message.outbound_payload["_alovia_sequence_index"]),
    )
    async with sessions() as session:
        repository = OutboundTaskRepository(session)
        async with session.begin():
            await repository.mark_sent(ordered[0].id, "wamid.preventive.0")
            await repository.mark_sent(ordered[1].id, "wamid.preventive.1")

    async with sessions() as session:
        sent_outreach = await session.get(CustomerOutreach, outreach.id)
        conversation = await session.get(Conversation, outreach.conversation_id)

    assert sent_outreach is not None
    assert sent_outreach.status == "sent"
    assert conversation is not None
    assert conversation.context["cleaning_outreach_pending_response"] is True
    assert conversation.context["cleaning_outreach_id"] == str(outreach.id)

    async with sessions() as session:
        async with session.begin():
            await mark_outreach_response(
                session,
                business_id=business_id,
                conversation_id=outreach.conversation_id,
                body="não tenho interesse",
                occurred_at=reference + timedelta(minutes=5),
            )

    async with sessions() as session:
        declined = await session.get(CustomerOutreach, outreach.id)

    assert declined is not None
    assert declined.status == "declined"
    assert declined.responded_at == reference + timedelta(minutes=5)

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.booking.availability import PostgresBookingAvailabilityPort
from app.conversations.ports import SlotUnavailable
from app.models import Appointment, ScheduleBlock
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

    assert revision == "20260903_0006"
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

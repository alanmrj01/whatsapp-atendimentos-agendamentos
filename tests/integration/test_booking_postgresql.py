from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.booking.availability import PostgresBookingAvailabilityPort
from app.booking.domain import BookingRequirements, ServiceAddress
from app.conversations.ports import SlotUnavailable
from app.core.config import get_settings
from app.models import (
    Appointment,
    Business,
    Customer,
    Employee,
    EmployeeService,
    ScheduleBlock,
    Service,
    WorkingHours,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
LOCAL_TEST_DATABASE_HOSTS = {"127.0.0.1", "::1", "localhost"}
SUPABASE_HOST_MARKERS = ("supabase.co", "supabase.com")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
    ),
]


def _async_url(value: str) -> str:
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


def _assert_disposable_database(value: str) -> None:
    url = make_url(_async_url(value))
    host = (url.host or "").casefold()
    database = (url.database or "").casefold()
    if "supabase" in host or any(
        marker in host for marker in SUPABASE_HOST_MARKERS
    ):
        raise RuntimeError(
            "TEST_DATABASE_URL não pode apontar para Supabase; use somente "
            "PostgreSQL local descartável"
        )
    if host not in LOCAL_TEST_DATABASE_HOSTS:
        raise RuntimeError(
            "TEST_DATABASE_URL deve usar host local loopback "
            "(localhost, 127.0.0.1 ou ::1)"
        )
    if "test" not in database:
        raise RuntimeError(
            "TEST_DATABASE_URL deve apontar para banco descartável com "
            "'test' no nome"
        )


@pytest.fixture(scope="module", autouse=True)
def migrated_test_database() -> Iterator[None]:
    if not TEST_DATABASE_URL:
        yield
        return
    _assert_disposable_database(TEST_DATABASE_URL)
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    try:
        yield
    finally:
        command.downgrade(config, "base")
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            for model in (
                Appointment,
                ScheduleBlock,
                WorkingHours,
                EmployeeService,
                Employee,
                Service,
                Customer,
                Business,
            ):
                await session.execute(delete(model))
    yield factory
    await engine.dispose()


async def seed_capacity(
    factory: async_sessionmaker[AsyncSession],
    *,
    employee_count: int = 1,
    duration_minutes: int = 60,
    travel_minutes: int = 0,
    working_start: time = time(7),
    working_end: time = time(19),
    customer_count: int = 2,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID], list[uuid.UUID], str]:
    business_id = uuid.uuid4()
    service_id = uuid.uuid4()
    employee_ids = [uuid.uuid4() for _ in range(employee_count)]
    customer_ids = [uuid.uuid4() for _ in range(customer_count)]
    local_date = datetime.now(ZoneInfo("America/Sao_Paulo")).date() + timedelta(
        days=2
    )
    async with factory() as session:
        async with session.begin():
            session.add(
                Business(
                    id=business_id,
                    name="Teste físico",
                    timezone="America/Sao_Paulo",
                    slot_interval_minutes=30,
                    service_origin_address=(
                        "Zona Leste de São José dos Campos - SP"
                    ),
                    service_origin_latitude=None,
                    service_origin_longitude=None,
                    service_origin_is_precise=False,
                    travel_calculation_method="configured_estimate",
                    default_travel_minutes=travel_minutes,
                    travel_fallback_allowed=True,
                    travel_route_provider=None,
                    travel_before_buffer_minutes=0,
                    travel_after_buffer_minutes=0,
                    travel_region_rules=[],
                    active=True,
                )
            )
            await session.flush()
            session.add(
                Service(
                    id=service_id,
                    business_id=business_id,
                    name="Serviço físico",
                    duration_minutes=duration_minutes,
                    base_price=Decimal("100.00"),
                    pricing_type="estimated",
                    automatic_booking=True,
                    included_quantity=1,
                    additional_unit_duration_minutes=0,
                    requires_address=True,
                    requires_quantity=False,
                    considers_difficult_access=False,
                    difficult_access_duration_minutes=0,
                    unknown_access_policy="conservative",
                    duration_margin_minutes=0,
                    asks_site_time_limit=False,
                    active=True,
                )
            )
            for index, customer_id in enumerate(customer_ids):
                session.add(
                    Customer(
                        id=customer_id,
                        business_id=business_id,
                        whatsapp_id=f"physical-customer-{uuid.uuid4()}-{index}",
                    )
                )
            for employee_id in employee_ids:
                session.add(
                    Employee(
                        id=employee_id,
                        business_id=business_id,
                        name="Recurso",
                        active=True,
                    )
                )
            await session.flush()
            for employee_id in employee_ids:
                session.add(
                    EmployeeService(
                        business_id=business_id,
                        employee_id=employee_id,
                        service_id=service_id,
                    )
                )
                session.add(
                    WorkingHours(
                        business_id=business_id,
                        employee_id=employee_id,
                        weekday=local_date.weekday(),
                        start_time=working_start,
                        end_time=working_end,
                    )
                )
    return (
        business_id,
        service_id,
        employee_ids,
        customer_ids,
        local_date.isoformat(),
    )


def requirements(key: str, *, site_end: time | None = None) -> BookingRequirements:
    return BookingRequirements(
        address=ServiceAddress("Rua do teste, 10, São José dos Campos - SP"),
        site_allowed_end=site_end,
        idempotency_key=key,
    )


async def confirm_in_new_transaction(
    factory: async_sessionmaker[AsyncSession],
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    service_id: uuid.UUID,
    selected_date: str,
    selected_time: str,
    key: str,
):  # type: ignore[no-untyped-def]
    async with factory() as session:
        async with session.begin():
            return await PostgresBookingAvailabilityPort(session).confirm(
                business_id,
                customer_id,
                service_id,
                selected_date,
                selected_time,
                requirements(key),
            )


async def test_a_one_employee_one_slot_allows_only_one_concurrent_booking(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = await seed_capacity(
        sessions, working_start=time(9), working_end=time(10)
    )

    results = await asyncio.gather(
        confirm_in_new_transaction(
            sessions,
            business_id,
            customers[0],
            service_id,
            selected_date,
            "09:00",
            "physical:a:1",
        ),
        confirm_in_new_transaction(
            sessions,
            business_id,
            customers[1],
            service_id,
            selected_date,
            "09:00",
            "physical:a:2",
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, SlotUnavailable) for value in results) == 1


async def test_b_two_employees_allow_two_concurrent_bookings(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = await seed_capacity(
        sessions,
        employee_count=2,
        working_start=time(9),
        working_end=time(10),
    )

    results = await asyncio.gather(
        *(
            confirm_in_new_transaction(
                sessions,
                business_id,
                customer_id,
                service_id,
                selected_date,
                "09:00",
                f"physical:b:{index}",
            )
            for index, customer_id in enumerate(customers)
        )
    )

    assert len({value.employee_id for value in results}) == 2


async def test_c_three_hour_service_can_end_inside_working_hours(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, _, selected_date = await seed_capacity(
        sessions,
        duration_minutes=180,
        working_start=time(8),
        working_end=time(17),
    )
    async with sessions() as session:
        port = PostgresBookingAvailabilityPort(session)
        ids = {
            option.id
            for option in await port.list_times(
                business_id,
                service_id,
                selected_date,
                requirements("unused"),
            )
        }

    assert "14:00" in ids


async def test_d_service_cannot_exceed_working_hours(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, _, selected_date = await seed_capacity(
        sessions,
        duration_minutes=180,
        working_start=time(8),
        working_end=time(17),
    )
    async with sessions() as session:
        ids = {
            option.id
            for option in await PostgresBookingAvailabilityPort(session).list_times(
                business_id,
                service_id,
                selected_date,
                requirements("unused"),
            )
        }

    assert "14:30" not in ids


async def test_e_site_restriction_is_enforced_physically(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, _, selected_date = await seed_capacity(
        sessions, duration_minutes=180
    )
    async with sessions() as session:
        ids = {
            option.id
            for option in await PostgresBookingAvailabilityPort(session).list_times(
                business_id,
                service_id,
                selected_date,
                requirements("unused", site_end=time(17)),
            )
        }

    assert "14:00" in ids
    assert "14:30" not in ids


async def test_f_travel_buffers_block_adjacent_capacity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = await seed_capacity(
        sessions, travel_minutes=30
    )
    await confirm_in_new_transaction(
        sessions,
        business_id,
        customers[0],
        service_id,
        selected_date,
        "10:00",
        "physical:f:1",
    )
    async with sessions() as session:
        ids = {
            option.id
            for option in await PostgresBookingAvailabilityPort(session).list_times(
                business_id,
                service_id,
                selected_date,
                requirements("unused"),
            )
        }

    assert "09:00" not in ids
    assert "11:00" not in ids
    assert "11:30" not in ids
    assert "12:00" in ids


async def test_g_exclude_constraint_is_final_authority(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, employees, customers, selected_date = (
        await seed_capacity(sessions)
    )
    local = datetime.combine(
        datetime.fromisoformat(selected_date).date(),
        time(10),
        ZoneInfo("America/Sao_Paulo"),
    ).astimezone(timezone.utc)
    first = _physical_appointment(
        business_id, customers[0], service_id, employees[0], local, "g:1"
    )
    second = _physical_appointment(
        business_id, customers[1], service_id, employees[0], local, "g:2"
    )
    async with sessions() as session:
        session.add(first)
        await session.commit()
    with pytest.raises(IntegrityError) as captured:
        async with sessions() as session:
            session.add(second)
            await session.commit()
    assert "excl_appointments_employee_confirmed_overlap" in str(captured.value)


async def test_h_transaction_rollback_removes_booking(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = await seed_capacity(
        sessions
    )
    with pytest.raises(RuntimeError):
        async with sessions() as session:
            async with session.begin():
                await PostgresBookingAvailabilityPort(session).confirm(
                    business_id,
                    customers[0],
                    service_id,
                    selected_date,
                    "10:00",
                    requirements("physical:h"),
                )
                raise RuntimeError("rollback")
    async with sessions() as session:
        count = await session.scalar(select(func.count(Appointment.id)))
    assert count == 0


async def test_i_concurrent_reschedule_keeps_loser_original(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = (
        await seed_capacity(sessions, employee_count=1)
    )
    first = await confirm_in_new_transaction(
        sessions,
        business_id,
        customers[0],
        service_id,
        selected_date,
        "09:00",
        "physical:i:1",
    )
    second = await confirm_in_new_transaction(
        sessions,
        business_id,
        customers[1],
        service_id,
        selected_date,
        "11:00",
        "physical:i:2",
    )

    async def move(customer_id: uuid.UUID, appointment_id: uuid.UUID):  # type: ignore[no-untyped-def]
        async with sessions() as session:
            async with session.begin():
                return await PostgresBookingAvailabilityPort(
                    session
                ).reschedule_booking_atomic(
                    business_id,
                    customer_id,
                    appointment_id,
                    selected_date,
                    "14:00",
                    requirements(f"physical:i:{appointment_id}"),
                )

    results = await asyncio.gather(
        move(customers[0], first.appointment_id),
        move(customers[1], second.appointment_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, SlotUnavailable) for value in results) == 1
    async with sessions() as session:
        appointments = (
            await session.scalars(
                select(Appointment).where(Appointment.business_id == business_id)
            )
        ).all()
    local_times = {
        value.starts_at.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%H:%M")
        for value in appointments
    }
    assert "14:00" in local_times
    assert local_times & {"09:00", "11:00"}
    assert all(value.status == "confirmed" for value in appointments)


async def test_j_concurrent_idempotency_creates_one_appointment(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    business_id, service_id, _, customers, selected_date = await seed_capacity(
        sessions, employee_count=2, customer_count=1
    )
    results = await asyncio.gather(
        *(
            confirm_in_new_transaction(
                sessions,
                business_id,
                customers[0],
                service_id,
                selected_date,
                "10:00",
                "physical:j:same",
            )
            for _ in range(2)
        )
    )
    async with sessions() as session:
        count = await session.scalar(select(func.count(Appointment.id)))

    assert count == 1
    assert results[0].appointment_id == results[1].appointment_id


def _physical_appointment(
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    service_id: uuid.UUID,
    employee_id: uuid.UUID,
    starts_at: datetime,
    key: str,
    *,
    travel_before_minutes: int = 0,
    travel_after_minutes: int = 0,
) -> Appointment:
    return Appointment(
        business_id=business_id,
        customer_id=customer_id,
        service_id=service_id,
        employee_id=employee_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status="confirmed",
        service_address={"address_line": "Rua física"},
        quantity=1,
        access_condition="normal",
        estimated_duration_minutes=60,
        travel_before_minutes=travel_before_minutes,
        travel_after_minutes=travel_after_minutes,
        estimated_price=Decimal("100.00"),
        pricing_type="estimated",
        estimate_details={},
        idempotency_key=key,
    )

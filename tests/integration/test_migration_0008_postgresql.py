from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Business, Customer, Employee, EmployeeService, Service
from app.operations.schemas import AppointmentCreate
from app.operations.service import OperationalService
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL,
    _assert_disposable_database,
    _async_url,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
)

ROLLBACK_KEY = "__alovia_migration_20260908_0008"
NOTE = "Nota que precisa sobreviver ao rollback"


async def _seed_pending_appointment() -> UUID:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id, customer_id, service_id, employee_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    try:
        async with factory() as session:
            async with session.begin():
                session.add_all([
                    Business(
                        id=business_id,
                        name="Roundtrip 0008",
                        timezone="America/Sao_Paulo",
                        active=True,
                    ),
                    Customer(
                        id=customer_id,
                        business_id=business_id,
                        whatsapp_id=f"roundtrip-{uuid4()}",
                        phone_e164="+5512999999000",
                        name="Cliente roundtrip",
                    ),
                    Service(
                        id=service_id,
                        business_id=business_id,
                        name="Serviço roundtrip",
                        duration_minutes=60,
                        base_price=Decimal("100.00"),
                        pricing_type="fixed",
                        active=True,
                    ),
                    Employee(
                        id=employee_id,
                        business_id=business_id,
                        name="Técnico roundtrip",
                        active=True,
                    ),
                ])
            async with session.begin():
                session.add(
                    EmployeeService(
                        business_id=business_id,
                        employee_id=employee_id,
                        service_id=service_id,
                    )
                )
            now = datetime.now(UTC) + timedelta(days=1)
            appointment = await OperationalService(session).create_appointment(
                business_id,
                AppointmentCreate(
                    customer_id=customer_id,
                    service_id=service_id,
                    employee_id=employee_id,
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                    status="pending",
                    notes=NOTE,
                ),
            )
            return appointment.id
    finally:
        await engine.dispose()


async def _read_0007_state(appointment_id: UUID) -> tuple[str, dict, int]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, estimate_details "
                        "FROM appointments WHERE id = :id"
                    ),
                    {"id": appointment_id},
                )
            ).mappings().one()
            notes_columns = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'appointments' AND column_name = 'notes'"
                )
            )
            return row["status"], row["estimate_details"], int(notes_columns or 0)
    finally:
        await engine.dispose()


async def _read_0008_state(appointment_id: UUID) -> tuple[str, str | None, dict]:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, notes, estimate_details "
                        "FROM appointments WHERE id = :id"
                    ),
                    {"id": appointment_id},
                )
            ).mappings().one()
            return row["status"], row["notes"], row["estimate_details"]
    finally:
        await engine.dispose()


def test_migration_0008_roundtrip_preserves_pending_status_and_notes() -> None:
    _assert_disposable_database(TEST_DATABASE_URL)
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    config = Config("alembic.ini")

    command.upgrade(config, "20260908_0008")
    appointment_id = asyncio.run(_seed_pending_appointment())

    command.downgrade(config, "20260904_0007")
    status, estimate_details, notes_columns = asyncio.run(
        _read_0007_state(appointment_id)
    )
    assert status == "cancelled"
    assert notes_columns == 0
    assert estimate_details[ROLLBACK_KEY] == {
        "migration": "20260908_0008",
        "status": "pending",
        "notes": NOTE,
    }

    command.upgrade(config, "20260908_0008")
    status, notes, estimate_details = asyncio.run(_read_0008_state(appointment_id))
    assert status == "pending"
    assert notes == NOTE
    assert ROLLBACK_KEY not in estimate_details

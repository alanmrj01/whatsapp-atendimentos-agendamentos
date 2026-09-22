from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
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
    """Seed using only columns that physically exist at migration 0008.

    Historical migration tests must not instantiate current ORM models, because
    those models legitimately gain columns in later revisions.
    """
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    business_id, customer_id, service_id, employee_id, appointment_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    now = datetime.now(UTC) + timedelta(days=1)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO businesses (id, name, timezone, active) "
                    "VALUES (:id, :name, :timezone, true)"
                ),
                {
                    "id": business_id,
                    "name": "Roundtrip 0008",
                    "timezone": "America/Sao_Paulo",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO customers "
                    "(id, business_id, whatsapp_id, phone_e164, name) "
                    "VALUES (:id, :business_id, :whatsapp_id, :phone, :name)"
                ),
                {
                    "id": customer_id,
                    "business_id": business_id,
                    "whatsapp_id": f"roundtrip-{uuid4()}",
                    "phone": "+5512999999000",
                    "name": "Cliente roundtrip",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO services "
                    "(id, business_id, name, duration_minutes, base_price, "
                    "pricing_type, automatic_booking, active) "
                    "VALUES (:id, :business_id, :name, 60, 100.00, "
                    "'fixed', true, true)"
                ),
                {
                    "id": service_id,
                    "business_id": business_id,
                    "name": "Serviço roundtrip",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO employees (id, business_id, name, active) "
                    "VALUES (:id, :business_id, :name, true)"
                ),
                {
                    "id": employee_id,
                    "business_id": business_id,
                    "name": "Técnico roundtrip",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO appointments ("
                    "id, business_id, customer_id, service_id, employee_id, "
                    "starts_at, ends_at, status, notes, quantity, "
                    "access_condition, estimated_duration_minutes, "
                    "travel_before_minutes, travel_after_minutes, "
                    "estimated_price, pricing_type, estimate_details"
                    ") VALUES ("
                    ":id, :business_id, :customer_id, :service_id, :employee_id, "
                    ":starts_at, :ends_at, 'pending', :notes, 1, 'normal', 60, "
                    "0, 0, 100.00, 'fixed', '{}'::jsonb"
                    ")"
                ),
                {
                    "id": appointment_id,
                    "business_id": business_id,
                    "customer_id": customer_id,
                    "service_id": service_id,
                    "employee_id": employee_id,
                    "starts_at": now,
                    "ends_at": now + timedelta(hours=1),
                    "notes": NOTE,
                },
            )
        return appointment_id
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

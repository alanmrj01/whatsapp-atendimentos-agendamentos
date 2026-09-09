from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    Appointment,
    Business,
    BusinessAccess,
    BusinessAutomationExclusion,
    BusinessUserMembership,
    BusinessWhatsAppConnection,
    Conversation,
    Customer,
    Employee,
    EmployeeService,
    Message,
    ProcessedWebhook,
    Service,
    WorkingHours,
)
from app.operations.schemas import (
    AppointmentCreate,
    AppointmentUpdate,
    EmployeeUpdate,
    WorkingHoursCreate,
)
from app.operations.service import OperationalService
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

ROLLBACK_KEY = "__alovia_migration_20260908_0008"


async def _clean(session: AsyncSession) -> None:
    for model in (
        Message,
        ProcessedWebhook,
        Appointment,
        WorkingHours,
        EmployeeService,
        Conversation,
        BusinessAutomationExclusion,
        BusinessWhatsAppConnection,
        BusinessUserMembership,
        BusinessAccess,
        Employee,
        Service,
        Customer,
        Business,
    ):
        await session.execute(delete(model))


@pytest.mark.usefixtures("migrated_test_database")
async def test_operational_data_is_real_tenant_scoped_and_mutable() -> None:
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_a, business_b = uuid4(), uuid4()
    customer_a, customer_b = uuid4(), uuid4()
    service_a, service_b = uuid4(), uuid4()
    employee_a, employee_b = uuid4(), uuid4()
    conversation_a, conversation_b = uuid4(), uuid4()
    now = datetime.now(UTC) + timedelta(hours=1)

    try:
        async with factory() as session:
            async with session.begin():
                await _clean(session)
                session.add_all([
                    Business(id=business_a, name="Empresa A", timezone="America/Sao_Paulo", active=True),
                    Business(id=business_b, name="Empresa B", timezone="America/Sao_Paulo", active=True),
                    Customer(id=customer_a, business_id=business_a, whatsapp_id="individual-a", phone_e164="+5512000000001", name="Cliente A"),
                    Customer(id=customer_b, business_id=business_b, whatsapp_id="individual-b", phone_e164="+5512000000002", name="Cliente B"),
                    Service(id=service_a, business_id=business_a, name="Serviço A", duration_minutes=60, base_price=Decimal("100.00"), pricing_type="fixed", active=True),
                    Service(id=service_b, business_id=business_b, name="Serviço B", duration_minutes=60, base_price=Decimal("100.00"), pricing_type="fixed", active=True),
                    Employee(id=employee_a, business_id=business_a, name="Técnico A", active=True),
                    Employee(id=employee_b, business_id=business_b, name="Técnico B", active=True),
                ])
            async with session.begin():
                session.add_all([
                    EmployeeService(business_id=business_a, employee_id=employee_a, service_id=service_a),
                    EmployeeService(business_id=business_b, employee_id=employee_b, service_id=service_b),
                    WorkingHours(business_id=business_a, employee_id=employee_a, weekday=now.astimezone().weekday(), start_time=time(0), end_time=time(23, 59)),
                    Conversation(id=conversation_a, business_id=business_a, customer_id=customer_a, state="MENU", handoff_status="none"),
                    Conversation(id=conversation_b, business_id=business_b, customer_id=customer_b, state="MENU", handoff_status="none"),
                ])
            async with session.begin():
                session.add_all([
                    Message(business_id=business_a, conversation_id=conversation_a, direction="inbound", message_type="text", body="Mensagem A", status="received"),
                    Message(business_id=business_b, conversation_id=conversation_b, direction="inbound", message_type="text", body="Mensagem B", status="received"),
                ])

            service = OperationalService(session)
            appointment = await service.create_appointment(
                business_a,
                AppointmentCreate(
                    customer_id=customer_a,
                    service_id=service_a,
                    employee_id=employee_a,
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                    notes="Observação operacional",
                ),
            )
            assert appointment.status == "pending"
            assert appointment.notes == "Observação operacional"

            appointment_date = now.astimezone(
                ZoneInfo("America/Sao_Paulo")
            ).date()
            listed = await service.list_appointments(
                business_a, selected_date=appointment_date
            )
            assert [item.id for item in listed] == [appointment.id]
            assert await service.list_appointments(business_b) == []

            with pytest.raises(HTTPException) as foreign_read:
                await service.get_appointment(business_b, appointment.id)
            assert foreign_read.value.status_code == 404
            with pytest.raises(HTTPException) as foreign_write:
                await service.update_appointment(
                    business_b, appointment.id, AppointmentUpdate(status="completed")
                )
            assert foreign_write.value.status_code == 404
            with pytest.raises(HTTPException) as foreign_reference:
                await service.create_appointment(
                    business_a,
                    AppointmentCreate(
                        customer_id=customer_b,
                        service_id=service_a,
                        employee_id=employee_a,
                        starts_at=now + timedelta(hours=2),
                        ends_at=now + timedelta(hours=3),
                    ),
                )
            assert foreign_reference.value.status_code == 404
            with pytest.raises(HTTPException) as foreign_employee:
                await service.update_employee(
                    business_b, employee_a, EmployeeUpdate(active=False)
                )
            assert foreign_employee.value.status_code == 404
            with pytest.raises(HTTPException) as foreign_hours:
                await service.create_working_hours(
                    business_a,
                    WorkingHoursCreate(
                        employee_id=employee_b,
                        weekday=0,
                        start_time=time(8),
                        end_time=time(18),
                    ),
                )
            assert foreign_hours.value.status_code == 404

            conversations_a, total_a = await service.list_conversations(
                business_a, search=None, status=None, page=1, page_size=25
            )
            conversations_b, total_b = await service.list_conversations(
                business_b, search=None, status=None, page=1, page_size=25
            )
            assert total_a == total_b == 1
            assert conversations_a[0].customer_name == "Cliente A"
            assert conversations_b[0].customer_name == "Cliente B"
            assert conversations_a[0].unread_count == 1

            dashboard = await service.dashboard_today(business_a)
            assert dashboard.metrics.waiting_count == 1
            setup = await service.setup_status(business_a)
            assert setup.company and setup.business_hours and setup.automation and setup.agenda
            assert not setup.whatsapp

            await service.update_appointment(
                business_a, appointment.id, AppointmentUpdate(status="cancelled")
            )
        async with factory() as session:
            async with session.begin():
                await _clean(session)
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("migrated_test_database")
async def test_migration_0008_roundtrip_preserves_pending_status_and_notes() -> None:
    """Exercise head -> 0007 -> 0008 with data introduced by 0008 present."""
    config = Config("alembic.ini")
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id, customer_id, service_id, employee_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    now = datetime.now(UTC) + timedelta(days=1)
    appointment_id = None

    try:
        async with factory() as session:
            async with session.begin():
                await _clean(session)
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

            appointment = await OperationalService(session).create_appointment(
                business_id,
                AppointmentCreate(
                    customer_id=customer_id,
                    service_id=service_id,
                    employee_id=employee_id,
                    starts_at=now,
                    ends_at=now + timedelta(hours=1),
                    status="pending",
                    notes="Nota que precisa sobreviver ao rollback",
                ),
            )
            appointment_id = appointment.id
            assert appointment.status == "pending"
            assert appointment.notes == "Nota que precisa sobreviver ao rollback"
    finally:
        await engine.dispose()

    try:
        command.downgrade(config, "20260904_0007")

        downgraded_engine = create_async_engine(
            _async_url(TEST_DATABASE_URL), pool_pre_ping=True
        )
        try:
            async with downgraded_engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT status, estimate_details FROM appointments WHERE id = :id"
                        ),
                        {"id": appointment_id},
                    )
                ).mappings().one()
                assert row["status"] == "cancelled"
                marker = row["estimate_details"][ROLLBACK_KEY]
                assert marker == {
                    "migration": "20260908_0008",
                    "status": "pending",
                    "notes": "Nota que precisa sobreviver ao rollback",
                }
                notes_columns = await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'appointments' AND column_name = 'notes'"
                    )
                )
                assert notes_columns == 0
        finally:
            await downgraded_engine.dispose()

        command.upgrade(config, "20260908_0008")

        restored_engine = create_async_engine(
            _async_url(TEST_DATABASE_URL), pool_pre_ping=True
        )
        try:
            async with restored_engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT status, notes, estimate_details "
                            "FROM appointments WHERE id = :id"
                        ),
                        {"id": appointment_id},
                    )
                ).mappings().one()
                assert row["status"] == "pending"
                assert row["notes"] == "Nota que precisa sobreviver ao rollback"
                assert ROLLBACK_KEY not in row["estimate_details"]
        finally:
            await restored_engine.dispose()
    finally:
        # Keep the module-scoped migrated_test_database fixture at head even if
        # an assertion above fails, so later integration tests do not inherit a
        # partially downgraded schema.
        command.upgrade(config, "20260908_0008")
        cleanup_engine = create_async_engine(
            _async_url(TEST_DATABASE_URL), pool_pre_ping=True
        )
        cleanup_factory = async_sessionmaker(cleanup_engine, expire_on_commit=False)
        try:
            async with cleanup_factory() as session:
                async with session.begin():
                    await _clean(session)
        finally:
            await cleanup_engine.dispose()

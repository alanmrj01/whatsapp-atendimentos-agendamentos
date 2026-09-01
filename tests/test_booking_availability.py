from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
from sqlalchemy.exc import IntegrityError

from app.booking.availability import (
    PostgresBookingAvailabilityPort,
    _CapacityData,
)
from app.booking.domain import (
    AccessCondition,
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceAddress,
    ServiceEstimate,
    TravelEstimate,
    TravelOrigin,
)
from app.conversations.ports import (
    BookingNotFound,
    BookingRequiresHandoff,
    SlotUnavailable,
)
from app.models import (
    Appointment,
    Business,
    ScheduleBlock,
    Service,
    WorkingHours,
)

BUSINESS_ID = uuid.UUID("a0000000-0000-0000-0000-000000000001")
SERVICE_ID = uuid.UUID("a0000000-0000-0000-0000-000000000002")
CUSTOMER_ID = uuid.UUID("a0000000-0000-0000-0000-000000000003")
EMPLOYEE_A = uuid.UUID("a0000000-0000-0000-0000-000000000004")
EMPLOYEE_B = uuid.UUID("a0000000-0000-0000-0000-000000000005")
APPOINTMENT_ID = uuid.UUID("a0000000-0000-0000-0000-000000000006")
NOW = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def business(**changes: Any) -> Business:
    values = Business(
        id=BUSINESS_ID,
        name="Empresa",
        timezone="America/Sao_Paulo",
        slot_interval_minutes=30,
        service_origin_address="Zona Leste de São José dos Campos - SP",
        service_origin_latitude=None,
        service_origin_longitude=None,
        service_origin_is_precise=False,
        travel_calculation_method="configured_estimate",
        default_travel_minutes=0,
        travel_fallback_allowed=True,
        travel_route_provider=None,
        travel_before_buffer_minutes=0,
        travel_after_buffer_minutes=0,
        travel_region_rules=[],
        active=True,
    )
    for key, value in changes.items():
        setattr(values, key, value)
    return values


def service() -> Service:
    return Service(
        id=SERVICE_ID,
        business_id=BUSINESS_ID,
        name="Serviço",
        duration_minutes=60,
        base_price=Decimal("100.00"),
        pricing_type="estimated",
        automatic_booking=True,
        included_quantity=1,
        additional_unit_duration_minutes=0,
        additional_unit_price=None,
        requires_address=True,
        requires_quantity=False,
        considers_difficult_access=False,
        difficult_access_duration_minutes=0,
        difficult_access_price=None,
        unknown_access_policy="conservative",
        duration_margin_minutes=0,
        asks_site_time_limit=False,
        active=True,
    )


def plan(
    *,
    duration: int = 60,
    before: int = 0,
    after: int = 0,
) -> BookingPlan:
    return BookingPlan(
        service=ServiceEstimate(
            estimated_duration_minutes=duration,
            estimated_price=Decimal("100.00"),
            pricing_type=PricingType.ESTIMATED,
            requires_human_quote=False,
            applied_rules=("base_duration",),
            qualifier="estimated",
        ),
        travel=TravelEstimate(
            travel_minutes=min(before, after),
            distance_km=None,
            source="test",
            method="test",
            estimated=True,
        ),
        travel_before_minutes=before,
        travel_after_minutes=after,
        requires_handoff=False,
    )


def working(
    employee_id: uuid.UUID,
    start: time,
    end: time,
    *,
    weekday: int = 2,
) -> WorkingHours:
    return WorkingHours(
        business_id=BUSINESS_ID,
        employee_id=employee_id,
        weekday=weekday,
        start_time=start,
        end_time=end,
    )


class StubAvailability(PostgresBookingAvailabilityPort):
    def __init__(
        self,
        *,
        capacity: _CapacityData,
        booking_plan: BookingPlan,
        company: Business | None = None,
    ) -> None:
        super().__init__(object(), now_provider=lambda: NOW)  # type: ignore[arg-type]
        self.capacity = capacity
        self.booking_plan = booking_plan
        self.company = company or business()
        self.catalog_service = service()

    async def _load_business_service(
        self, _: uuid.UUID, __: uuid.UUID
    ) -> tuple[Business, Service]:
        return self.company, self.catalog_service

    async def _build_plan(
        self,
        _: Business,
        __: Service,
        ___: BookingRequirements,
    ) -> BookingPlan:
        return self.booking_plan

    async def _load_capacity(self, *args: Any, **kwargs: Any) -> _CapacityData:
        return self.capacity


def capacity(
    *hours: WorkingHours,
    employees: tuple[uuid.UUID, ...] = (EMPLOYEE_A,),
    blocks: tuple[ScheduleBlock, ...] = (),
    appointments: tuple[Appointment, ...] = (),
) -> _CapacityData:
    return _CapacityData(employees, hours, blocks, appointments)


@pytest.mark.asyncio
async def test_service_must_fit_one_of_multiple_working_ranges() -> None:
    port = StubAvailability(
        capacity=capacity(
            working(EMPLOYEE_A, time(7), time(12)),
            working(EMPLOYEE_A, time(13), time(17)),
        ),
        booking_plan=plan(duration=180),
    )

    times = await port.list_times(BUSINESS_ID, SERVICE_ID, "2026-09-02")
    ids = {option.id for option in times}

    assert "09:00" in ids
    assert "10:00" not in ids
    assert "14:00" in ids
    assert "11:30" not in ids


@pytest.mark.asyncio
async def test_travel_before_and_after_reserve_operational_capacity() -> None:
    port = StubAvailability(
        capacity=capacity(working(EMPLOYEE_A, time(8), time(12))),
        booking_plan=plan(duration=60, before=30, after=30),
    )

    ids = {
        option.id
        for option in await port.list_times(
            BUSINESS_ID, SERVICE_ID, "2026-09-02"
        )
    }

    assert "08:00" not in ids
    assert "08:30" in ids
    assert "10:30" in ids
    assert "11:00" not in ids


@pytest.mark.asyncio
async def test_site_limit_applies_to_service_end_not_travel_after() -> None:
    port = StubAvailability(
        capacity=capacity(working(EMPLOYEE_A, time(8), time(18))),
        booking_plan=plan(duration=180, after=30),
    )
    requirements = BookingRequirements(site_allowed_end=time(17))

    ids = {
        option.id
        for option in await port.list_times(
            BUSINESS_ID,
            SERVICE_ID,
            "2026-09-02",
            requirements,
        )
    }

    assert "14:00" in ids
    assert "14:30" not in ids


@pytest.mark.asyncio
async def test_schedule_block_and_confirmed_appointment_block_slots() -> None:
    block = ScheduleBlock(
        business_id=BUSINESS_ID,
        employee_id=EMPLOYEE_A,
        starts_at=datetime(2026, 9, 2, 13, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 2, 14, tzinfo=timezone.utc),
    )
    confirmed = appointment(
        starts_at=datetime(2026, 9, 2, 15, tzinfo=timezone.utc),
        status="confirmed",
    )
    port = StubAvailability(
        capacity=capacity(
            working(EMPLOYEE_A, time(8), time(17)),
            blocks=(block,),
            appointments=(confirmed,),
        ),
        booking_plan=plan(),
    )

    ids = {
        option.id
        for option in await port.list_times(
            BUSINESS_ID, SERVICE_ID, "2026-09-02"
        )
    }

    assert "10:00" not in ids
    assert "12:00" not in ids
    assert "14:00" in ids


@pytest.mark.asyncio
async def test_cancelled_and_completed_appointments_do_not_block() -> None:
    cancelled = appointment(
        starts_at=datetime(2026, 9, 2, 15, tzinfo=timezone.utc),
        status="cancelled",
    )
    completed = appointment(
        starts_at=datetime(2026, 9, 2, 16, tzinfo=timezone.utc),
        status="completed",
    )
    port = StubAvailability(
        capacity=capacity(
            working(EMPLOYEE_A, time(8), time(17)),
            appointments=(cancelled, completed),
        ),
        booking_plan=plan(),
    )

    ids = {
        option.id
        for option in await port.list_times(
            BUSINESS_ID, SERVICE_ID, "2026-09-02"
        )
    }

    assert "12:00" in ids
    assert "13:00" in ids


@pytest.mark.asyncio
async def test_multiple_employees_produce_one_customer_time() -> None:
    port = StubAvailability(
        capacity=capacity(
            working(EMPLOYEE_A, time(8), time(10)),
            working(EMPLOYEE_B, time(8), time(10)),
            employees=(EMPLOYEE_A, EMPLOYEE_B),
        ),
        booking_plan=plan(),
    )

    times = await port.list_times(BUSINESS_ID, SERVICE_ID, "2026-09-02")

    assert [option.id for option in times].count("08:00") == 1
    assert all(str(EMPLOYEE_A) not in option.id for option in times)


@pytest.mark.asyncio
async def test_past_slots_are_not_returned_in_business_timezone() -> None:
    port = StubAvailability(
        capacity=capacity(
            working(EMPLOYEE_A, time(6), time(12), weekday=1)
        ),
        booking_plan=plan(),
    )

    ids = {
        option.id
        for option in await port.list_times(
            BUSINESS_ID, SERVICE_ID, "2026-09-01"
        )
    }

    assert "06:00" not in ids
    assert "07:30" in ids


@pytest.mark.asyncio
async def test_dates_have_portuguese_labels_without_os_locale() -> None:
    port = StubAvailability(
        capacity=capacity(working(EMPLOYEE_A, time(8), time(10))),
        booking_plan=plan(),
    )

    dates = await port.list_dates(BUSINESS_ID, SERVICE_ID)

    assert dates[0].id == "2026-09-02"
    assert dates[0].label == "quarta, 2 de setembro"


def appointment(*, starts_at: datetime, status: str) -> Appointment:
    return Appointment(
        id=uuid.uuid4(),
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        service_id=SERVICE_ID,
        employee_id=EMPLOYEE_A,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=status,
        service_address={"address_line": "Rua Teste"},
        quantity=1,
        access_condition="normal",
        estimated_duration_minutes=60,
        travel_before_minutes=0,
        travel_after_minutes=0,
        estimated_price=Decimal("100.00"),
        pricing_type="estimated",
        estimate_details={},
    )


class MutationSession:
    def __init__(self) -> None:
        self.added: list[Appointment] = []
        self.flushes = 0

    @asynccontextmanager
    async def begin_nested(self):  # type: ignore[no-untyped-def]
        yield

    def add(self, value: Appointment) -> None:
        if value.id is None:
            value.id = APPOINTMENT_ID
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, _: Appointment) -> None:
        return None


class MutationPort(PostgresBookingAvailabilityPort):
    def __init__(self, session: MutationSession, existing: Appointment | None = None):
        super().__init__(session)  # type: ignore[arg-type]
        self.company = business()
        self.catalog_service = service()
        self.booking_plan = plan(duration=90, before=20, after=25)
        self.existing = existing

    async def _load_business_service(
        self, _: uuid.UUID, __: uuid.UUID
    ) -> tuple[Business, Service]:
        return self.company, self.catalog_service

    async def _build_plan(
        self, _: Business, __: Service, ___: BookingRequirements
    ) -> BookingPlan:
        return self.booking_plan

    async def _employees_for_exact_start(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        return (EMPLOYEE_A,), datetime(2026, 9, 2, 12, tzinfo=timezone.utc)

    async def _find_idempotent_appointment(
        self, _: uuid.UUID, __: uuid.UUID, ___: str
    ) -> Appointment | None:
        return self.existing

    async def _lock_customer_appointment(
        self, _: uuid.UUID, __: uuid.UUID, ___: uuid.UUID
    ) -> Appointment:
        if self.existing is None:
            raise AssertionError("test appointment missing")
        return self.existing


@pytest.mark.asyncio
async def test_confirmation_freezes_duration_travel_price_and_address() -> None:
    session = MutationSession()
    port = MutationPort(session)
    requirements = BookingRequirements(
        quantity=2,
        access_condition=AccessCondition.DIFFICULT,
        address=ServiceAddress("Rua Congelada, 123"),
        site_allowed_end=time(18),
        idempotency_key="booking:test",
    )

    confirmation = await port.confirm(
        BUSINESS_ID,
        CUSTOMER_ID,
        SERVICE_ID,
        "2026-09-02",
        "09:00",
        requirements,
    )

    stored = session.added[0]
    assert confirmation.appointment_id == APPOINTMENT_ID
    assert stored.estimated_duration_minutes == 90
    assert stored.travel_before_minutes == 20
    assert stored.travel_after_minutes == 25
    assert stored.estimated_price == Decimal("100.00")
    assert stored.service_address == {"address_line": "Rua Congelada, 123"}
    assert stored.idempotency_key == "booking:test"


@pytest.mark.asyncio
async def test_confirmation_is_defensively_idempotent() -> None:
    existing = appointment(
        starts_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        status="confirmed",
    )
    existing.idempotency_key = "booking:test"
    session = MutationSession()
    port = MutationPort(session, existing)

    confirmation = await port.confirm(
        BUSINESS_ID,
        CUSTOMER_ID,
        SERVICE_ID,
        "2026-09-02",
        "09:00",
        BookingRequirements(idempotency_key="booking:test"),
    )

    assert confirmation.appointment_id == existing.id
    assert session.added == []


@pytest.mark.asyncio
async def test_cancel_is_scoped_and_idempotent_without_deleting_history() -> None:
    existing = appointment(
        starts_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        status="confirmed",
    )
    session = MutationSession()
    port = MutationPort(session, existing)

    await port.cancel_booking(BUSINESS_ID, CUSTOMER_ID, existing.id)
    await port.cancel_booking(BUSINESS_ID, CUSTOMER_ID, existing.id)

    assert existing.status == "cancelled"
    assert session.flushes == 1


@pytest.mark.asyncio
async def test_failed_reschedule_keeps_original_appointment_intact() -> None:
    existing = appointment(
        starts_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        status="confirmed",
    )
    original = (existing.employee_id, existing.starts_at, existing.ends_at)
    port = MutationPort(MutationSession(), existing)

    async def unavailable(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        raise SlotUnavailable("occupied")

    port._employees_for_exact_start = unavailable  # type: ignore[method-assign]
    with pytest.raises(SlotUnavailable):
        await port.reschedule_booking_atomic(
            BUSINESS_ID,
            CUSTOMER_ID,
            existing.id,
            "2026-09-03",
            "10:00",
            BookingRequirements(),
        )

    assert (existing.employee_id, existing.starts_at, existing.ends_at) == original
    assert existing.status == "confirmed"


@pytest.mark.asyncio
async def test_no_eligible_employee_is_fail_closed() -> None:
    class EmptyResult:
        def all(self) -> list[uuid.UUID]:
            return []

    class EmptySession:
        async def scalars(self, _: object) -> EmptyResult:
            return EmptyResult()

    port = PostgresBookingAvailabilityPort(EmptySession())  # type: ignore[arg-type]

    with pytest.raises(BookingRequiresHandoff):
        await port._require_eligible_employees(BUSINESS_ID, SERVICE_ID)


@pytest.mark.asyncio
async def test_service_query_filters_active_and_automatically_bookable_catalog() -> None:
    class Rows:
        def all(self):  # type: ignore[no-untyped-def]
            return [(SERVICE_ID, "Serviço ativo")]

    class CaptureSession:
        statement: object | None = None

        async def execute(self, statement: object) -> Rows:
            self.statement = statement
            return Rows()

    session = CaptureSession()
    options = await PostgresBookingAvailabilityPort(  # type: ignore[arg-type]
        session
    ).list_services(BUSINESS_ID)
    sql = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql_dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert options[0].label == "Serviço ativo"
    assert "services.active is true" in sql
    assert "businesses.active is true" in sql
    assert "employees.active is true" in sql
    assert "services.automatic_booking is true" in sql
    assert "services.pricing_type = 'human_quote'" in sql


@pytest.mark.asyncio
async def test_booking_plan_uses_configurable_origin_and_buffers() -> None:
    class RecordingTravel:
        origin: TravelOrigin | None = None

        async def estimate(self, origin: TravelOrigin, destination: ServiceAddress):  # type: ignore[no-untyped-def]
            self.origin = origin
            return TravelEstimate(
                travel_minutes=20,
                distance_km=None,
                source="test",
                method="recording",
                estimated=True,
            )

    travel = RecordingTravel()
    company = business(
        service_origin_address="Origem configurada pela empresa",
        travel_calculation_method="route",
        travel_before_buffer_minutes=5,
        travel_after_buffer_minutes=10,
    )

    class PlanPort(PostgresBookingAvailabilityPort):
        async def _load_business_service(self, *args: Any):  # type: ignore[no-untyped-def]
            return company, service()

        async def _require_eligible_employees(self, *args: Any):  # type: ignore[no-untyped-def]
            return (EMPLOYEE_A,)

    port = PlanPort(object(), travel_time_port=travel)  # type: ignore[arg-type]
    booking_plan = await port.estimate(
        BUSINESS_ID,
        SERVICE_ID,
        BookingRequirements(address=ServiceAddress("Destino")),
    )

    assert travel.origin is not None
    assert travel.origin.address == "Origem configurada pela empresa"
    assert travel.origin.is_precise is False
    assert booking_plan.travel_before_minutes == 25
    assert booking_plan.travel_after_minutes == 30
    assert booking_plan.travel.estimated is True


@pytest.mark.asyncio
async def test_route_without_provider_or_allowed_fallback_requires_handoff() -> None:
    company = business(
        travel_calculation_method="route",
        default_travel_minutes=None,
        travel_fallback_allowed=False,
    )

    class PlanPort(PostgresBookingAvailabilityPort):
        async def _load_business_service(self, *args: Any):  # type: ignore[no-untyped-def]
            return company, service()

        async def _require_eligible_employees(self, *args: Any):  # type: ignore[no-untyped-def]
            return (EMPLOYEE_A,)

    port = PlanPort(object())  # type: ignore[arg-type]
    booking_plan = await port.estimate(
        BUSINESS_ID,
        SERVICE_ID,
        BookingRequirements(address=ServiceAddress("Destino")),
    )

    assert booking_plan.requires_handoff is True
    assert booking_plan.handoff_reason == "travel_estimate_unavailable"
    assert booking_plan.travel.available is False


@pytest.mark.asyncio
async def test_confirm_raises_slot_unavailable_without_persisting() -> None:
    session = MutationSession()
    port = MutationPort(session)

    async def unavailable(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        raise SlotUnavailable("occupied")

    port._employees_for_exact_start = unavailable  # type: ignore[method-assign]
    with pytest.raises(SlotUnavailable):
        await port.confirm(
            BUSINESS_ID,
            CUSTOMER_ID,
            SERVICE_ID,
            "2026-09-02",
            "09:00",
            BookingRequirements(),
        )
    assert session.added == []


@pytest.mark.asyncio
async def test_only_known_exclusion_integrity_error_becomes_slot_unavailable() -> None:
    class DatabaseError(Exception):
        def __init__(self, constraint_name: str) -> None:
            self.constraint_name = constraint_name

    class FailingSession(MutationSession):
        def __init__(self, constraint_name: str) -> None:
            super().__init__()
            self.constraint_name = constraint_name

        async def flush(self) -> None:
            raise IntegrityError(
                "insert",
                {},
                DatabaseError(self.constraint_name),
            )

    known = MutationPort(
        FailingSession("excl_appointments_employee_confirmed_overlap")
    )
    with pytest.raises(SlotUnavailable):
        await known.confirm(
            BUSINESS_ID,
            CUSTOMER_ID,
            SERVICE_ID,
            "2026-09-02",
            "09:00",
            BookingRequirements(),
        )

    unrelated = MutationPort(FailingSession("some_other_constraint"))
    with pytest.raises(IntegrityError):
        await unrelated.confirm(
            BUSINESS_ID,
            CUSTOMER_ID,
            SERVICE_ID,
            "2026-09-02",
            "09:00",
            BookingRequirements(),
        )


@pytest.mark.asyncio
async def test_cancel_query_is_scoped_by_business_customer_and_appointment() -> None:
    class ScalarSession:
        statement: object | None = None

        async def scalar(self, statement: object):  # type: ignore[no-untyped-def]
            self.statement = statement
            return None

    session = ScalarSession()
    with pytest.raises(BookingNotFound):
        await PostgresBookingAvailabilityPort(  # type: ignore[arg-type]
            session
        ).cancel_booking(BUSINESS_ID, CUSTOMER_ID, APPOINTMENT_ID)
    sql = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql_dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "appointments.id =" in sql
    assert "appointments.business_id =" in sql
    assert "appointments.customer_id =" in sql
    assert "for update" in sql

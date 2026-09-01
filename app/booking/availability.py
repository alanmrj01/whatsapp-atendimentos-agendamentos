from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.domain import (
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceConfiguration,
    ServiceEstimate,
    ServiceIntake,
    TravelCalculationMethod,
    TravelEstimate,
    TravelOrigin,
    UnknownAccessPolicy,
)
from app.booking.estimator import ServiceEstimator
from app.booking.travel import (
    ConfiguredTravelTimePort,
    TravelTimePort,
    unavailable_travel_estimate,
)
from app.conversations.ports import (
    BookingConfirmation,
    BookingNotFound,
    BookingOption,
    BookingRequiresHandoff,
    SlotUnavailable,
)
from app.models import (
    Appointment,
    Business,
    Employee,
    EmployeeService,
    ScheduleBlock,
    Service,
    WorkingHours,
)

AVAILABILITY_HORIZON_DAYS = 30
APPOINTMENT_EXCLUSION_CONSTRAINT = (
    "excl_appointments_employee_confirmed_overlap"
)
APPOINTMENT_IDEMPOTENCY_INDEX = "uq_appointments_idempotency_key_present"
PORTUGUESE_WEEKDAYS = (
    "segunda",
    "terça",
    "quarta",
    "quinta",
    "sexta",
    "sábado",
    "domingo",
)
PORTUGUESE_MONTHS = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


@dataclass(frozen=True, slots=True)
class _CapacityData:
    employee_ids: tuple[uuid.UUID, ...]
    working_hours: tuple[WorkingHours, ...]
    blocks: tuple[ScheduleBlock, ...]
    appointments: tuple[Appointment, ...]


class PostgresBookingAvailabilityPort:
    def __init__(
        self,
        session: AsyncSession,
        *,
        estimator: ServiceEstimator | None = None,
        travel_time_port: TravelTimePort | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.estimator = estimator or ServiceEstimator()
        self.travel_time_port = travel_time_port
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    async def list_services(
        self,
        business_id: uuid.UUID,
    ) -> Sequence[BookingOption]:
        eligible_employee = exists(
            select(EmployeeService.employee_id)
            .join(
                Employee,
                and_(
                    Employee.business_id == EmployeeService.business_id,
                    Employee.id == EmployeeService.employee_id,
                ),
            )
            .where(
                EmployeeService.business_id == business_id,
                EmployeeService.service_id == Service.id,
                Employee.active.is_(True),
            )
        )
        rows = await self.session.execute(
            select(Service.id, Service.name)
            .join(Business, Business.id == Service.business_id)
            .where(
                Service.business_id == business_id,
                Service.active.is_(True),
                Business.active.is_(True),
                (Service.automatic_booking.is_(True))
                | (Service.pricing_type == PricingType.HUMAN_QUOTE.value),
                eligible_employee,
            )
            .order_by(Service.name, Service.id)
        )
        return tuple(
            BookingOption(id=str(service_id), label=name)
            for service_id, name in rows.all()
        )

    async def get_service_intake(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
    ) -> ServiceIntake:
        _, service = await self._load_business_service(
            business_id, service_id
        )
        await self._require_eligible_employees(business_id, service_id)
        try:
            pricing_type = PricingType(service.pricing_type)
        except ValueError:
            raise BookingRequiresHandoff("Invalid service configuration") from None
        return ServiceIntake(
            requires_quantity=service.requires_quantity,
            requires_address=service.requires_address,
            considers_difficult_access=service.considers_difficult_access,
            asks_site_time_limit=service.asks_site_time_limit,
            automatic_booking=service.automatic_booking,
            pricing_type=pricing_type,
        )

    async def estimate(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
        requirements: BookingRequirements,
    ) -> BookingPlan:
        business, service = await self._load_business_service(
            business_id, service_id
        )
        await self._require_eligible_employees(business_id, service_id)
        return await self._build_plan(business, service, requirements)

    async def list_dates(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
        requirements: BookingRequirements = BookingRequirements(),
    ) -> Sequence[BookingOption]:
        business, service = await self._load_business_service(
            business_id, service_id
        )
        plan = await self._build_plan(business, service, requirements)
        self._require_automatic_plan(plan)
        local_now = self._local_now(business.timezone)
        first_date = local_now.date()
        last_date = first_date + timedelta(days=AVAILABILITY_HORIZON_DAYS - 1)
        starts = await self._available_starts(
            business,
            service,
            requirements,
            plan,
            first_date,
            last_date,
        )
        available_dates = sorted({value.date() for value in starts})
        return tuple(
            BookingOption(
                id=value.isoformat(),
                label=_portuguese_date_label(value),
            )
            for value in available_dates
        )

    async def list_times(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        requirements: BookingRequirements = BookingRequirements(),
    ) -> Sequence[BookingOption]:
        parsed_date = _parse_date(selected_date)
        business, service = await self._load_business_service(
            business_id, service_id
        )
        plan = await self._build_plan(business, service, requirements)
        self._require_automatic_plan(plan)
        starts = await self._available_starts(
            business,
            service,
            requirements,
            plan,
            parsed_date,
            parsed_date,
        )
        labels = sorted({value.strftime("%H:%M") for value in starts})
        return tuple(BookingOption(id=value, label=value) for value in labels)

    async def confirm(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        selected_time: str,
        requirements: BookingRequirements = BookingRequirements(),
    ) -> BookingConfirmation:
        if requirements.idempotency_key:
            existing = await self._find_idempotent_appointment(
                business_id,
                customer_id,
                requirements.idempotency_key,
            )
            if existing is not None:
                return _confirmation(existing)

        parsed_date = _parse_date(selected_date)
        parsed_time = _parse_time(selected_time)
        business, service = await self._load_business_service(
            business_id, service_id
        )
        plan = await self._build_plan(business, service, requirements)
        self._require_automatic_plan(plan)
        employee_ids, starts_at = await self._employees_for_exact_start(
            business,
            service,
            requirements,
            plan,
            parsed_date,
            parsed_time,
        )
        ends_at = starts_at + timedelta(
            minutes=plan.service.estimated_duration_minutes
        )
        for employee_id in employee_ids:
            appointment = Appointment(
                business_id=business_id,
                customer_id=customer_id,
                service_id=service_id,
                employee_id=employee_id,
                starts_at=starts_at,
                ends_at=ends_at,
                status="confirmed",
            )
            self._apply_snapshot(appointment, requirements, plan)
            try:
                async with self.session.begin_nested():
                    self.session.add(appointment)
                    await self.session.flush()
            except IntegrityError as exc:
                if _has_constraint(exc, APPOINTMENT_EXCLUSION_CONSTRAINT):
                    continue
                if (
                    requirements.idempotency_key
                    and _has_constraint(exc, APPOINTMENT_IDEMPOTENCY_INDEX)
                ):
                    existing = await self._find_idempotent_appointment(
                        business_id,
                        customer_id,
                        requirements.idempotency_key,
                    )
                    if existing is not None:
                        return _confirmation(existing)
                raise
            return _confirmation(appointment)
        raise SlotUnavailable("Selected slot is unavailable")

    async def cancel_booking(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        appointment_id: uuid.UUID,
    ) -> BookingConfirmation:
        appointment = await self._lock_customer_appointment(
            business_id, customer_id, appointment_id
        )
        if appointment.status == "confirmed":
            appointment.status = "cancelled"
            await self.session.flush()
        elif appointment.status != "cancelled":
            raise BookingNotFound("Appointment cannot be cancelled")
        return _confirmation(appointment)

    async def reschedule_booking_atomic(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        appointment_id: uuid.UUID,
        selected_date: str,
        selected_time: str,
        requirements: BookingRequirements,
    ) -> BookingConfirmation:
        appointment = await self._lock_customer_appointment(
            business_id, customer_id, appointment_id
        )
        if appointment.status != "confirmed":
            raise BookingNotFound("Appointment cannot be rescheduled")

        parsed_date = _parse_date(selected_date)
        parsed_time = _parse_time(selected_time)
        business, service = await self._load_business_service(
            business_id, appointment.service_id
        )
        plan = await self._build_plan(business, service, requirements)
        self._require_automatic_plan(plan)
        employee_ids, starts_at = await self._employees_for_exact_start(
            business,
            service,
            requirements,
            plan,
            parsed_date,
            parsed_time,
            exclude_appointment_id=appointment.id,
        )
        ends_at = starts_at + timedelta(
            minutes=plan.service.estimated_duration_minutes
        )
        original_idempotency_key = appointment.idempotency_key

        for employee_id in employee_ids:
            try:
                async with self.session.begin_nested():
                    appointment.employee_id = employee_id
                    appointment.starts_at = starts_at
                    appointment.ends_at = ends_at
                    self._apply_snapshot(appointment, requirements, plan)
                    appointment.idempotency_key = original_idempotency_key
                    await self.session.flush()
            except IntegrityError as exc:
                if not _has_constraint(exc, APPOINTMENT_EXCLUSION_CONSTRAINT):
                    raise
                await self.session.refresh(appointment)
                continue
            return _confirmation(appointment)
        await self.session.refresh(appointment)
        raise SlotUnavailable("Selected slot is unavailable")

    async def _load_business_service(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
    ) -> tuple[Business, Service]:
        row = (
            await self.session.execute(
                select(Business, Service)
                .join(Service, Service.business_id == Business.id)
                .where(
                    Business.id == business_id,
                    Business.active.is_(True),
                    Service.id == service_id,
                    Service.active.is_(True),
                )
            )
        ).one_or_none()
        if row is None:
            raise BookingRequiresHandoff("Service is unavailable")
        return row[0], row[1]

    async def _require_eligible_employees(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        rows = await self.session.scalars(
            select(Employee.id)
            .join(
                EmployeeService,
                and_(
                    EmployeeService.business_id == Employee.business_id,
                    EmployeeService.employee_id == Employee.id,
                ),
            )
            .where(
                Employee.business_id == business_id,
                Employee.active.is_(True),
                EmployeeService.service_id == service_id,
            )
            .order_by(Employee.id)
        )
        employee_ids = tuple(rows.all())
        if not employee_ids:
            raise BookingRequiresHandoff("No eligible capacity")
        return employee_ids

    async def _build_plan(
        self,
        business: Business,
        service: Service,
        requirements: BookingRequirements,
    ) -> BookingPlan:
        try:
            configuration = ServiceConfiguration(
                duration_minutes=service.duration_minutes,
                base_price=(
                    Decimal(service.base_price)
                    if service.base_price is not None
                    else None
                ),
                pricing_type=PricingType(service.pricing_type),
                automatic_booking=service.automatic_booking,
                included_quantity=service.included_quantity,
                additional_unit_duration_minutes=(
                    service.additional_unit_duration_minutes
                ),
                additional_unit_price=(
                    Decimal(service.additional_unit_price)
                    if service.additional_unit_price is not None
                    else None
                ),
                requires_address=service.requires_address,
                requires_quantity=service.requires_quantity,
                considers_difficult_access=service.considers_difficult_access,
                difficult_access_duration_minutes=(
                    service.difficult_access_duration_minutes
                ),
                difficult_access_price=(
                    Decimal(service.difficult_access_price)
                    if service.difficult_access_price is not None
                    else None
                ),
                unknown_access_policy=UnknownAccessPolicy(
                    service.unknown_access_policy
                ),
                duration_margin_minutes=service.duration_margin_minutes,
            )
        except (TypeError, ValueError):
            raise BookingRequiresHandoff("Invalid service configuration") from None

        service_estimate = self.estimator.estimate(configuration, requirements)
        if service_estimate.requires_human_quote:
            travel = _zero_travel_estimate()
        elif requirements.address is None:
            travel = _zero_travel_estimate()
        else:
            try:
                origin = TravelOrigin(
                    address=business.service_origin_address,
                    latitude=(
                        Decimal(business.service_origin_latitude)
                        if business.service_origin_latitude is not None
                        else None
                    ),
                    longitude=(
                        Decimal(business.service_origin_longitude)
                        if business.service_origin_longitude is not None
                        else None
                    ),
                    is_precise=business.service_origin_is_precise,
                )
                configured_port = ConfiguredTravelTimePort(
                    fallback_minutes=business.default_travel_minutes,
                    fallback_allowed=business.travel_fallback_allowed,
                    region_rules=business.travel_region_rules,
                )
                calculation_method = TravelCalculationMethod(
                    business.travel_calculation_method
                )
            except ValueError:
                origin = TravelOrigin(
                    address="Operational origin unavailable",
                    is_precise=False,
                )
                travel = unavailable_travel_estimate(origin)
            else:
                if (
                    calculation_method is TravelCalculationMethod.ROUTE
                    and self.travel_time_port is not None
                ):
                    travel = await self.travel_time_port.estimate(
                        origin,
                        requirements.address,
                    )
                    travel = replace(
                        travel,
                        estimated=travel.estimated or not origin.is_precise,
                        origin_is_precise=origin.is_precise,
                    )
                else:
                    travel = await configured_port.estimate(
                        origin,
                        requirements.address,
                    )

        requires_handoff = (
            service_estimate.requires_human_quote
            or not travel.available
            or not travel.within_service_area
        )
        reason = None
        if service_estimate.requires_human_quote:
            reason = "service_estimate_requires_human_quote"
        elif not travel.available:
            reason = "travel_estimate_unavailable"
        elif not travel.within_service_area:
            reason = "address_outside_service_area"
        return BookingPlan(
            service=service_estimate,
            travel=travel,
            travel_before_minutes=(
                travel.travel_minutes + business.travel_before_buffer_minutes
            ),
            travel_after_minutes=(
                travel.travel_minutes + business.travel_after_buffer_minutes
            ),
            requires_handoff=requires_handoff,
            handoff_reason=reason,
        )

    async def _available_starts(
        self,
        business: Business,
        service: Service,
        requirements: BookingRequirements,
        plan: BookingPlan,
        first_date: date,
        last_date: date,
        *,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> dict[datetime, tuple[uuid.UUID, ...]]:
        timezone_info = _timezone(business.timezone)
        utc_start = datetime.combine(
            first_date, time.min, timezone_info
        ).astimezone(timezone.utc)
        utc_end = datetime.combine(
            last_date + timedelta(days=1), time.min, timezone_info
        ).astimezone(timezone.utc)
        capacity = await self._load_capacity(
            business.id,
            service.id,
            utc_start,
            utc_end,
            exclude_appointment_id=exclude_appointment_id,
        )
        now = self.now_provider()
        if now.tzinfo is None:
            raise ValueError("now_provider must return an aware datetime")

        starts: dict[datetime, set[uuid.UUID]] = defaultdict(set)
        day = first_date
        while day <= last_date:
            for working_range in capacity.working_hours:
                if working_range.weekday != day.weekday():
                    continue
                operational_start = datetime.combine(
                    day, working_range.start_time, timezone_info
                )
                operational_end = datetime.combine(
                    day, working_range.end_time, timezone_info
                )
                earliest_service_start = operational_start + timedelta(
                    minutes=plan.travel_before_minutes
                )
                latest_service_start = operational_end - timedelta(
                    minutes=(
                        plan.service.estimated_duration_minutes
                        + plan.travel_after_minutes
                    )
                )
                candidate = _ceil_to_slot(
                    earliest_service_start,
                    business.slot_interval_minutes,
                )
                while candidate <= latest_service_start:
                    service_end = candidate + timedelta(
                        minutes=plan.service.estimated_duration_minutes
                    )
                    occupied_start = candidate - timedelta(
                        minutes=plan.travel_before_minutes
                    )
                    occupied_end = service_end + timedelta(
                        minutes=plan.travel_after_minutes
                    )
                    if (
                        candidate.astimezone(timezone.utc) > now
                        and self._within_site_limit(
                            day, service_end, requirements.site_allowed_end
                        )
                        and not self._has_conflict(
                            working_range.employee_id,
                            occupied_start.astimezone(timezone.utc),
                            occupied_end.astimezone(timezone.utc),
                            capacity,
                        )
                    ):
                        starts[candidate].add(working_range.employee_id)
                    candidate += timedelta(minutes=business.slot_interval_minutes)
            day += timedelta(days=1)
        return {
            start: tuple(sorted(employee_ids, key=str))
            for start, employee_ids in starts.items()
        }

    async def _load_capacity(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
        utc_start: datetime,
        utc_end: datetime,
        *,
        exclude_appointment_id: uuid.UUID | None,
    ) -> _CapacityData:
        employee_ids = await self._require_eligible_employees(
            business_id, service_id
        )
        working_hours = tuple(
            (
                await self.session.scalars(
                    select(WorkingHours).where(
                        WorkingHours.business_id == business_id,
                        WorkingHours.employee_id.in_(employee_ids),
                    )
                )
            ).all()
        )
        blocks = tuple(
            (
                await self.session.scalars(
                    select(ScheduleBlock).where(
                        ScheduleBlock.business_id == business_id,
                        ScheduleBlock.employee_id.in_(employee_ids),
                        ScheduleBlock.starts_at < utc_end,
                        ScheduleBlock.ends_at > utc_start,
                    )
                )
            ).all()
        )
        appointment_query = select(Appointment).where(
            Appointment.business_id == business_id,
            Appointment.employee_id.in_(employee_ids),
            Appointment.status == "confirmed",
            Appointment.starts_at
            - func.make_interval(
                0, 0, 0, 0, 0, Appointment.travel_before_minutes, 0
            )
            < utc_end,
            Appointment.ends_at
            + func.make_interval(
                0, 0, 0, 0, 0, Appointment.travel_after_minutes, 0
            )
            > utc_start,
        )
        if exclude_appointment_id is not None:
            appointment_query = appointment_query.where(
                Appointment.id != exclude_appointment_id
            )
        appointments = tuple(
            (await self.session.scalars(appointment_query)).all()
        )
        return _CapacityData(
            employee_ids=employee_ids,
            working_hours=working_hours,
            blocks=blocks,
            appointments=appointments,
        )

    async def _employees_for_exact_start(
        self,
        business: Business,
        service: Service,
        requirements: BookingRequirements,
        plan: BookingPlan,
        selected_date: date,
        selected_time: time,
        *,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> tuple[tuple[uuid.UUID, ...], datetime]:
        starts = await self._available_starts(
            business,
            service,
            requirements,
            plan,
            selected_date,
            selected_date,
            exclude_appointment_id=exclude_appointment_id,
        )
        local_start = datetime.combine(
            selected_date, selected_time, _timezone(business.timezone)
        )
        employee_ids = starts.get(local_start)
        if not employee_ids:
            raise SlotUnavailable("Selected slot is unavailable")
        return employee_ids, local_start.astimezone(timezone.utc)

    @staticmethod
    def _within_site_limit(
        selected_date: date,
        service_end: datetime,
        site_allowed_end: time | None,
    ) -> bool:
        if site_allowed_end is None:
            return True
        allowed_end = datetime.combine(
            selected_date, site_allowed_end, service_end.tzinfo
        )
        return service_end <= allowed_end

    @staticmethod
    def _has_conflict(
        employee_id: uuid.UUID,
        occupied_start: datetime,
        occupied_end: datetime,
        capacity: _CapacityData,
    ) -> bool:
        for block in capacity.blocks:
            if block.employee_id == employee_id and _overlaps(
                occupied_start, occupied_end, block.starts_at, block.ends_at
            ):
                return True
        for appointment in capacity.appointments:
            if (
                appointment.employee_id != employee_id
                or appointment.status != "confirmed"
            ):
                continue
            existing_start = appointment.starts_at - timedelta(
                minutes=appointment.travel_before_minutes
            )
            existing_end = appointment.ends_at + timedelta(
                minutes=appointment.travel_after_minutes
            )
            if _overlaps(
                occupied_start, occupied_end, existing_start, existing_end
            ):
                return True
        return False

    async def _find_idempotent_appointment(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        idempotency_key: str,
    ) -> Appointment | None:
        return await self.session.scalar(
            select(Appointment).where(
                Appointment.business_id == business_id,
                Appointment.customer_id == customer_id,
                Appointment.idempotency_key == idempotency_key,
            )
        )

    async def _lock_customer_appointment(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        appointment_id: uuid.UUID,
    ) -> Appointment:
        appointment = await self.session.scalar(
            select(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.business_id == business_id,
                Appointment.customer_id == customer_id,
            )
            .with_for_update()
        )
        if appointment is None:
            raise BookingNotFound("Appointment was not found")
        return appointment

    @staticmethod
    def _apply_snapshot(
        appointment: Appointment,
        requirements: BookingRequirements,
        plan: BookingPlan,
    ) -> None:
        appointment.service_address = (
            requirements.address.to_snapshot()
            if requirements.address is not None
            else None
        )
        appointment.quantity = requirements.quantity or 1
        appointment.access_condition = requirements.access_condition.value
        appointment.estimated_duration_minutes = (
            plan.service.estimated_duration_minutes
        )
        appointment.travel_before_minutes = plan.travel_before_minutes
        appointment.travel_after_minutes = plan.travel_after_minutes
        appointment.estimated_price = plan.service.estimated_price
        appointment.pricing_type = plan.service.pricing_type.value
        appointment.estimate_details = plan.snapshot_details()
        appointment.site_allowed_end = requirements.site_allowed_end
        appointment.idempotency_key = requirements.idempotency_key

    def _local_now(self, timezone_name: str) -> datetime:
        now = self.now_provider()
        if now.tzinfo is None:
            raise ValueError("now_provider must return an aware datetime")
        return now.astimezone(_timezone(timezone_name))

    @staticmethod
    def _require_automatic_plan(plan: BookingPlan) -> None:
        if plan.requires_handoff:
            raise BookingRequiresHandoff(
                plan.handoff_reason or "Booking requires human assistance"
            )


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SlotUnavailable("Invalid date") from None
    if parsed.isoformat() != value:
        raise SlotUnavailable("Invalid date")
    return parsed


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        raise SlotUnavailable("Invalid time") from None
    if parsed.strftime("%H:%M") != value or parsed.tzinfo is not None:
        raise SlotUnavailable("Invalid time")
    return parsed


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise BookingRequiresHandoff("Invalid business timezone") from None


def _ceil_to_slot(value: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0:
        raise BookingRequiresHandoff("Invalid slot interval")
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = (value - midnight).total_seconds() / 60
    rounded_minutes = math.ceil(elapsed_minutes / interval_minutes) * interval_minutes
    return midnight + timedelta(minutes=rounded_minutes)


def _overlaps(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> bool:
    return first_start < second_end and first_end > second_start


def _portuguese_date_label(value: date) -> str:
    return (
        f"{PORTUGUESE_WEEKDAYS[value.weekday()]}, {value.day} de "
        f"{PORTUGUESE_MONTHS[value.month - 1]}"
    )


def _zero_travel_estimate() -> TravelEstimate:
    return TravelEstimate(
        travel_minutes=0,
        distance_km=None,
        source="not_required",
        method="no_travel",
        estimated=False,
    )


def _confirmation(appointment: Appointment) -> BookingConfirmation:
    return BookingConfirmation(
        appointment_id=appointment.id,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        employee_id=appointment.employee_id,
    )


def _has_constraint(exc: IntegrityError, expected: str) -> bool:
    original = exc.orig
    errors = (
        original,
        getattr(original, "__cause__", None),
        getattr(original, "__context__", None),
    )
    return any(
        getattr(error, "constraint_name", None) == expected
        for error in errors
        if error is not None
    )

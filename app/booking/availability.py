from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.domain import (
    AccessCondition,
    AddressResolution,
    AddressResolutionStatus,
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceConfiguration,
    ServiceAddress,
    ServiceEstimate,
    ServiceIntake,
    TravelCalculationMethod,
    TravelEstimate,
    TravelOrigin,
    UnknownAccessPolicy,
)
from app.booking.estimator import ServiceEstimator
from app.booking.google_maps import AddressValidationPort
from app.booking.travel import (
    ConfiguredTravelTimePort,
    TravelTimePort,
    same_address_travel_estimate,
    unavailable_travel_estimate,
)
from app.conversations.service_semantics import generate_service_intent_examples
from app.conversations.ports import (
    BookingConfirmation,
    ExistingBooking,
    BookingNotFound,
    BookingOption,
    BookingRecoveryRequired,
    BookingRequiresHandoff,
    SlotUnavailable,
)
from app.models import (
    Appointment,
    Business,
    BusinessCatalogItem,
    BusinessNotification,
    Employee,
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

BRAZIL_NATIONAL_FIXED_HOLIDAYS = {
    (1, 1),   # Confraternização Universal
    (4, 21),  # Tiradentes
    (5, 1),   # Dia do Trabalho
    (9, 7),   # Independência
    (10, 12), # Nossa Senhora Aparecida
    (11, 2),  # Finados
    (11, 15), # Proclamação da República
    (11, 20), # Consciência Negra
    (12, 25), # Natal
}


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
        address_validation_port: AddressValidationPort | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.estimator = estimator or ServiceEstimator()
        self.travel_time_port = travel_time_port
        self.address_validation_port = address_validation_port
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._candidate_plans: dict[
            tuple[uuid.UUID, datetime], BookingPlan
        ] = {}

    async def list_services(
        self,
        business_id: uuid.UUID,
    ) -> Sequence[BookingOption]:
        rows = await self.session.execute(
            select(Service.id, Service.name, Service.intent_examples)
            .join(Business, Business.id == Service.business_id)
            .where(
                Service.business_id == business_id,
                Service.active.is_(True),
                Business.active.is_(True),
                (Service.automatic_booking.is_(True))
                | (Service.pricing_type == PricingType.HUMAN_QUOTE.value),
            )
            .order_by(Service.name, Service.id)
        )
        return tuple(
            BookingOption(
                id=str(service_id),
                label=name,
                examples=tuple(
                    example
                    for example in (
                        intent_examples
                        or generate_service_intent_examples(name)
                    )
                    if isinstance(example, str)
                ),
            )
            for service_id, name, intent_examples in rows.all()
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
            raise BookingRecoveryRequired("service_configuration_invalid") from None
        return ServiceIntake(
            requires_quantity=service.requires_quantity,
            requires_address=service.requires_address,
            considers_difficult_access=service.considers_difficult_access,
            asks_site_time_limit=service.asks_site_time_limit,
            asks_tubing_length=service.asks_tubing_length,
            automatic_booking=service.automatic_booking,
            pricing_type=pricing_type,
        )

    async def resolve_service_address(
        self,
        business_id: uuid.UUID,
        raw_address: str,
    ) -> AddressResolution:
        business = await self.session.scalar(
            select(Business).where(
                Business.id == business_id,
                Business.active.is_(True),
            )
        )
        if business is None:
            raise BookingRecoveryRequired("business_unavailable")
        if self.address_validation_port is None:
            return AddressResolution(
                AddressResolutionStatus.ACCEPTED,
                address=ServiceAddress(address_line=raw_address),
            )
        return await self.address_validation_port.resolve(
            raw_address,
            default_city=business.service_origin_city,
            default_state=business.service_origin_state,
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
        for employee_id in employee_ids:
            employee_plan = self._candidate_plans.get(
                (employee_id, starts_at),
                plan,
            )
            ends_at = starts_at + timedelta(
                minutes=employee_plan.service.estimated_duration_minutes
            )
            appointment = Appointment(
                business_id=business_id,
                customer_id=customer_id,
                service_id=service_id,
                employee_id=employee_id,
                starts_at=starts_at,
                ends_at=ends_at,
                status="confirmed",
            )
            self._apply_snapshot(appointment, requirements, employee_plan)
            try:
                async with self.session.begin_nested():
                    self.session.add(appointment)
                    await self.session.flush()
                    self.session.add(
                        BusinessNotification(
                            business_id=business_id,
                            appointment_id=appointment.id,
                            event_type="automatic_booking_confirmed",
                        )
                    )
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

    async def list_customer_bookings(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> Sequence[ExistingBooking]:
        business = await self.session.scalar(
            select(Business).where(
                Business.id == business_id,
                Business.active.is_(True),
            )
        )
        if business is None:
            return ()
        rows = await self.session.execute(
            select(Appointment, Service.name)
            .join(
                Service,
                and_(
                    Service.business_id == Appointment.business_id,
                    Service.id == Appointment.service_id,
                ),
            )
            .where(
                Appointment.business_id == business_id,
                Appointment.customer_id == customer_id,
                Appointment.status == "confirmed",
                Appointment.starts_at > self.now_provider(),
            )
            .order_by(Appointment.starts_at)
            .limit(10)
        )
        timezone_info = _timezone(business.timezone)
        bookings: list[ExistingBooking] = []
        for appointment, service_name in rows.all():
            try:
                access = AccessCondition(appointment.access_condition or "normal")
            except ValueError:
                access = AccessCondition.UNKNOWN
            address = ServiceAddress.from_snapshot(appointment.service_address)
            requirements = BookingRequirements(
                quantity=appointment.quantity or 1,
                access_condition=access,
                address=address,
                site_allowed_end=appointment.site_allowed_end,
                tubing_meters=(
                    Decimal(appointment.tubing_meters)
                    if appointment.tubing_meters is not None
                    else None
                ),
            )
            local_start = appointment.starts_at.astimezone(timezone_info)
            label = (
                f"{service_name} · {local_start.strftime('%d/%m às %H:%M')}"
            )
            bookings.append(
                ExistingBooking(
                    appointment_id=appointment.id,
                    service_id=appointment.service_id,
                    label=label,
                    requirements=requirements,
                )
            )
        return tuple(bookings)

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
            raise BookingRecoveryRequired("service_unavailable")
        return row[0], row[1]

    async def _require_eligible_employees(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        # Small service businesses usually do not maintain skill matrices.
        # Any active technician can be allocated unless a future explicit
        # restriction is introduced.
        rows = await self.session.scalars(
            select(Employee.id)
            .where(
                Employee.business_id == business_id,
                Employee.active.is_(True),
                Employee.operational_role == "technician",
            )
            .order_by(Employee.id)
        )
        employee_ids = tuple(rows.all())
        if not employee_ids:
            raise BookingRecoveryRequired("no_active_technician")
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
            raise BookingRecoveryRequired("service_configuration_invalid") from None

        service_estimate = self.estimator.estimate(configuration, requirements)
        service_estimate = await self._apply_catalog_additions(
            business.id,
            service,
            requirements,
            service_estimate,
        )
        if service_estimate.requires_human_quote:
            travel = _zero_travel_estimate()
        elif requirements.address is None:
            travel = _zero_travel_estimate()
        else:
            try:
                if not business.service_origin_address:
                    raise ValueError("Operational origin is not configured")
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
                travel = unavailable_travel_estimate(
                    origin,
                    reason="origin_configuration_unavailable",
                )
            else:
                same_address = same_address_travel_estimate(
                    origin,
                    requirements.address,
                )
                if same_address is not None:
                    travel = same_address
                elif (
                    self.travel_time_port is not None
                    and (
                        calculation_method is TravelCalculationMethod.ROUTE
                        or not business.travel_fallback_allowed
                    )
                ):
                    travel = await self.travel_time_port.estimate(
                        origin,
                        requirements.address,
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
            reason = travel.failure_reason or "travel_estimate_unavailable"
        elif not travel.within_service_area:
            reason = "address_outside_service_area"
        preparation, finishing, interval = _operational_buffers(
            business,
            requirements,
            service_estimate,
        )
        interval_before = interval // 2
        interval_after = interval - interval_before
        return BookingPlan(
            service=service_estimate,
            travel=travel,
            travel_before_minutes=(
                travel.travel_minutes
                + business.travel_before_buffer_minutes
                + preparation
                + interval_before
            ),
            travel_after_minutes=(
                travel.travel_minutes
                + business.travel_after_buffer_minutes
                + finishing
                + interval_after
            ),
            requires_handoff=requires_handoff,
            handoff_reason=reason,
        )

    async def _apply_catalog_additions(
        self,
        business_id: uuid.UUID,
        service: Service,
        requirements: BookingRequirements,
        estimate: ServiceEstimate,
    ) -> ServiceEstimate:
        if (
            not service.asks_tubing_length
            or requirements.tubing_meters is None
            or service.included_tubing_meters is None
        ):
            return estimate

        extra_meters = requirements.tubing_meters - Decimal(service.included_tubing_meters)
        if extra_meters <= 0:
            return estimate

        item = await self.session.scalar(
            select(BusinessCatalogItem).where(
                BusinessCatalogItem.business_id == business_id,
                BusinessCatalogItem.preset_key == "extra-tubing-meter",
                BusinessCatalogItem.active.is_(True),
            )
        )
        if item is None or item.price is None:
            return replace(
                estimate,
                requires_human_quote=True,
                qualifier="Preço da tubulação adicional não configurado.",
                applied_rules=(
                    *estimate.applied_rules,
                    "extra_tubing_requires_human_quote",
                ),
            )

        additional_price = extra_meters * Decimal(item.price)
        base_price = estimate.estimated_price
        if base_price is None:
            return replace(
                estimate,
                requires_human_quote=True,
                qualifier="Preço base do serviço não configurado.",
                applied_rules=(
                    *estimate.applied_rules,
                    "base_price_required_for_extra_tubing",
                ),
            )
        return replace(
            estimate,
            estimated_price=base_price + additional_price,
            applied_rules=(
                *estimate.applied_rules,
                f"extra_tubing_meters:{extra_meters}",
                f"extra_tubing_price:{additional_price}",
            ),
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
        local_now = now.astimezone(timezone_info)
        notice_minutes = business.minimum_booking_notice_minutes
        if notice_minutes is None:
            notice_minutes = _automatic_booking_notice_minutes(
                service,
                requirements,
                plan.service,
            )
        earliest_allowed_start = local_now + timedelta(minutes=notice_minutes)

        starts: dict[datetime, set[uuid.UUID]] = defaultdict(set)
        self._candidate_plans = {}
        day = first_date
        while day <= last_date:
            for employee_id, range_start, range_end in self._operating_ranges_for_day(
                business, day, capacity
            ):
                operational_start = datetime.combine(day, range_start, timezone_info)
                operational_end = datetime.combine(day, range_end, timezone_info)
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
                    candidate_plan = await self._schedule_aware_plan(
                        business,
                        requirements,
                        plan,
                        employee_id,
                        candidate,
                        service_end,
                        capacity.appointments,
                    )
                    occupied_start = candidate - timedelta(
                        minutes=candidate_plan.travel_before_minutes
                    )
                    occupied_end = service_end + timedelta(
                        minutes=candidate_plan.travel_after_minutes
                    )
                    if (
                        candidate > earliest_allowed_start
                        and not candidate_plan.requires_handoff
                        and self._within_site_limit(
                            day, service_end, requirements.site_allowed_end
                        )
                        and not self._has_conflict(
                            employee_id,
                            occupied_start.astimezone(timezone.utc),
                            occupied_end.astimezone(timezone.utc),
                            capacity,
                        )
                    ):
                        starts[candidate].add(employee_id)
                        self._candidate_plans[
                            (
                                employee_id,
                                candidate.astimezone(timezone.utc),
                            )
                        ] = candidate_plan
                    candidate += timedelta(minutes=business.slot_interval_minutes)
            day += timedelta(days=1)
        return {
            start: tuple(
                sorted(
                    employee_ids,
                    key=lambda employee_id: (
                        self._candidate_plans[
                            (employee_id, start.astimezone(timezone.utc))
                        ].travel_before_minutes
                        + self._candidate_plans[
                            (employee_id, start.astimezone(timezone.utc))
                        ].travel_after_minutes,
                        str(employee_id),
                    ),
                )
            )
            for start, employee_ids in starts.items()
        }

    @staticmethod
    def _operating_ranges_for_day(
        business: Business,
        selected_date: date,
        capacity: _CapacityData,
    ) -> tuple[tuple[uuid.UUID, time, time], ...]:
        configured_weekdays = {
            int(value)
            for value in (business.operating_weekdays or [])
            if isinstance(value, int) and 0 <= value <= 4
        }
        company_hours_configured = bool(
            configured_weekdays
            and business.weekday_start_time is not None
            and business.weekday_end_time is not None
        )
        if not company_hours_configured:
            return tuple(
                (item.employee_id, item.start_time, item.end_time)
                for item in capacity.working_hours
                if item.weekday == selected_date.weekday()
            )

        weekend_or_holiday = (
            selected_date.weekday() >= 5
            or _is_brazil_national_holiday(selected_date)
        )
        if weekend_or_holiday:
            if (
                not business.weekend_holiday_enabled
                or business.weekend_holiday_start_time is None
                or business.weekend_holiday_end_time is None
            ):
                return ()
            start_time = business.weekend_holiday_start_time
            end_time = business.weekend_holiday_end_time
        else:
            if selected_date.weekday() not in configured_weekdays:
                return ()
            start_time = business.weekday_start_time
            end_time = business.weekday_end_time

        return tuple(
            (employee_id, start_time, end_time)
            for employee_id in capacity.employee_ids
        )

    async def _schedule_aware_plan(
        self,
        business: Business,
        requirements: BookingRequirements,
        plan: BookingPlan,
        employee_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        appointments: tuple[Appointment, ...],
    ) -> BookingPlan:
        """Reserve travel from the technician's real adjacent commitments.

        The first service of the local day starts from the configured business
        origin. Later services start at the previous customer's address. When
        another service follows, the candidate also has to leave enough time
        to reach that next address. Missing route inputs fail closed.
        """

        destination = requirements.address
        if destination is None:
            return plan

        timezone_info = _timezone(business.timezone)
        local_day = starts_at.astimezone(timezone_info).date()
        employee_appointments = tuple(
            item
            for item in appointments
            if item.employee_id == employee_id
            and item.status == "confirmed"
            and item.starts_at.astimezone(timezone_info).date() == local_day
        )
        previous = max(
            (item for item in employee_appointments if item.ends_at <= starts_at),
            key=lambda item: item.ends_at,
            default=None,
        )
        following = min(
            (item for item in employee_appointments if item.starts_at >= ends_at),
            key=lambda item: item.starts_at,
            default=None,
        )

        if previous is None:
            before = plan.travel
        else:
            previous_address = ServiceAddress.from_snapshot(
                previous.service_address
            )
            if previous_address is None:
                before = unavailable_travel_estimate(
                    TravelOrigin(
                        address="Previous service location unavailable",
                        is_precise=False,
                    )
                )
            else:
                before = await self._estimate_travel_leg(
                    business,
                    TravelOrigin(
                        address=previous_address.searchable_text,
                        is_precise=False,
                    ),
                    destination,
                )

        if following is None:
            after = _zero_travel_estimate()
        else:
            following_address = ServiceAddress.from_snapshot(
                following.service_address
            )
            if following_address is None:
                after = unavailable_travel_estimate(
                    TravelOrigin(
                        address=destination.searchable_text,
                        is_precise=False,
                    )
                )
            else:
                after = await self._estimate_travel_leg(
                    business,
                    TravelOrigin(
                        address=destination.searchable_text,
                        is_precise=False,
                    ),
                    following_address,
                )

        preparation, finishing, interval = _operational_buffers(
            business,
            requirements,
            plan.service,
        )
        interval_before = interval // 2
        interval_after = interval - interval_before
        available = before.available and after.available
        within_service_area = (
            before.within_service_area and after.within_service_area
        )
        combined = TravelEstimate(
            travel_minutes=max(before.travel_minutes, after.travel_minutes),
            distance_km=None,
            source=f"{before.source}|{after.source}",
            method=f"{before.method}|{after.method}",
            estimated=before.estimated or after.estimated,
            within_service_area=within_service_area,
            available=available,
            origin_is_precise=before.origin_is_precise,
        )
        reason = plan.handoff_reason
        if not available:
            reason = "travel_estimate_unavailable"
        elif not within_service_area:
            reason = "address_outside_service_area"
        return BookingPlan(
            service=plan.service,
            travel=combined,
            travel_before_minutes=(
                before.travel_minutes
                + business.travel_before_buffer_minutes
                + preparation
                + interval_before
            ),
            travel_after_minutes=(
                after.travel_minutes
                + business.travel_after_buffer_minutes
                + finishing
                + interval_after
            ),
            requires_handoff=(
                plan.service.requires_human_quote
                or not available
                or not within_service_area
            ),
            handoff_reason=reason,
        )

    async def _estimate_travel_leg(
        self,
        business: Business,
        origin: TravelOrigin,
        destination: ServiceAddress,
    ) -> TravelEstimate:
        same_address = same_address_travel_estimate(origin, destination)
        if same_address is not None:
            return same_address
        configured_port = ConfiguredTravelTimePort(
            fallback_minutes=business.default_travel_minutes,
            fallback_allowed=business.travel_fallback_allowed,
            region_rules=business.travel_region_rules,
        )
        try:
            method = TravelCalculationMethod(business.travel_calculation_method)
        except ValueError:
            return unavailable_travel_estimate(origin)
        if self.travel_time_port and (
            method is TravelCalculationMethod.ROUTE
            or not business.travel_fallback_allowed
        ):
            return await self.travel_time_port.estimate(
                origin,
                destination,
            )
        return await configured_port.estimate(origin, destination)

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
        appointment.tubing_meters = requirements.tubing_meters
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
        if not plan.requires_handoff:
            return
        reason = plan.handoff_reason or "booking_requires_human_assistance"
        if reason in {
            "travel_estimate_unavailable",
            "route_temporarily_unavailable",
            "route_not_found",
            "route_invalid_response",
            "origin_address_unavailable",
            "destination_address_unavailable",
            "origin_configuration_unavailable",
            "address_outside_service_area",
        }:
            raise BookingRecoveryRequired(reason)
        raise BookingRequiresHandoff(reason)


def _is_brazil_national_holiday(value: date) -> bool:
    return (value.month, value.day) in BRAZIL_NATIONAL_FIXED_HOLIDAYS


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
        raise BookingRecoveryRequired("schedule_configuration_invalid") from None


def _ceil_to_slot(value: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0:
        raise BookingRecoveryRequired("schedule_configuration_invalid")
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


def _operational_buffers(
    business: Business,
    requirements: BookingRequirements,
    service_estimate: ServiceEstimate,
) -> tuple[int, int, int]:
    """Return preparation, finishing and between-service buffers.

    Blank business settings intentionally mean "automatic by ALOVIA". Automatic
    operational buffers are bounded to 50 minutes total; travel is calculated
    separately and does not consume this cap.
    """

    quantity = max(1, requirements.quantity or 1)
    difficult = requirements.access_condition.value == "difficult"

    automatic_preparation = min(
        25,
        8 + min(12, max(0, quantity - 1) * 4) + (8 if difficult else 0),
    )
    automatic_finishing = 8 if service_estimate.estimated_duration_minutes < 180 else 12
    automatic_interval = 5 if service_estimate.estimated_duration_minutes < 120 else 10

    preparation = (
        business.preparation_minutes
        if business.preparation_minutes is not None
        else automatic_preparation
    )
    finishing = (
        business.finishing_minutes
        if business.finishing_minutes is not None
        else automatic_finishing
    )
    interval = (
        business.interval_between_services_minutes
        if business.interval_between_services_minutes is not None
        else automatic_interval
    )

    auto_values = [
        business.preparation_minutes is None,
        business.finishing_minutes is None,
        business.interval_between_services_minutes is None,
    ]
    if any(auto_values):
        total = preparation + finishing + interval
        if total > 50:
            overflow = total - 50
            if business.interval_between_services_minutes is None:
                reduction = min(interval, overflow)
                interval -= reduction
                overflow -= reduction
            if overflow and business.finishing_minutes is None:
                reduction = min(finishing, overflow)
                finishing -= reduction
                overflow -= reduction
            if overflow and business.preparation_minutes is None:
                preparation = max(0, preparation - overflow)

    return preparation, finishing, interval


def _automatic_booking_notice_minutes(
    service: Service,
    requirements: BookingRequirements,
    service_estimate: ServiceEstimate,
) -> int:
    quantity = max(1, requirements.quantity or 1)
    notice = 120
    if service_estimate.estimated_duration_minutes >= 180:
        notice = 180
    if quantity >= 3 or requirements.access_condition.value == "difficult":
        notice = max(notice, 240)
    return notice


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

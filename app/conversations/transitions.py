from __future__ import annotations

import uuid
import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from typing import Any

from app.booking.domain import (
    AccessCondition,
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceAddress,
    ServiceIntake,
)

from app.conversations.constants import (
    ALLOWED_CONTEXT_KEYS,
    ACCESS_DIFFICULT,
    ACCESS_NORMAL,
    ACCESS_UNKNOWN,
    BOOKING_BACK,
    BOOKING_CANCEL,
    BOOKING_CONFIRM,
    MENU_BOOK,
    MENU_CANCEL,
    MENU_HUMAN,
    MENU_RESCHEDULE,
    SITE_LIMIT_17,
    SITE_LIMIT_18,
    SITE_LIMIT_NONE,
    ConversationState,
)
from app.conversations.outbound import (
    OutboundMessage,
    access_selection_message,
    address_request_message,
    booking_cancelled_message,
    booking_completed_message,
    booking_confirmation_message,
    booking_unavailable_message,
    cancel_message,
    date_selection_message,
    handoff_message,
    main_menu_message,
    no_services_message,
    quantity_selection_message,
    reschedule_message,
    service_selection_message,
    site_limit_message,
    slot_unavailable_message,
    time_selection_message,
)
from app.conversations.ports import (
    BookingAvailabilityPort,
    BookingConfirmation,
    BookingOption,
    BookingPortUnavailable,
    BookingRequiresHandoff,
    SlotUnavailable,
)
from app.conversations.types import (
    ConversationInput,
    ConversationSnapshot,
    ConversationTransition,
)


async def determine_transition(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition | None:
    if not conversation.automation_enabled:
        return None

    state = _canonical_state(conversation.state)
    context = _clean_context(conversation.context)
    action = inbound.interactive_id

    if state in {ConversationState.START, ConversationState.COMPLETED}:
        return _transition(ConversationState.MENU, {}, main_menu_message())
    if state is ConversationState.HUMAN_HANDOFF:
        return None
    if state is ConversationState.MENU:
        return await _handle_menu(inbound, action, booking_port)
    if state is ConversationState.BOOKING_SERVICE:
        return await _handle_service(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_QUANTITY:
        return await _handle_quantity(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_ACCESS:
        return await _handle_access(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_ADDRESS:
        return await _handle_address(inbound, context, booking_port)
    if state is ConversationState.BOOKING_SITE_LIMIT:
        return await _handle_site_limit(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_DATE:
        return await _handle_date(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_TIME:
        return await _handle_time(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_CONFIRM:
        return await _handle_confirmation(inbound, context, action, booking_port)
    if state is ConversationState.RESCHEDULE:
        if action == BOOKING_BACK:
            return _transition(ConversationState.MENU, {}, main_menu_message())
        return _transition(state, {}, reschedule_message())
    if state is ConversationState.CANCEL:
        if action == BOOKING_BACK:
            return _transition(ConversationState.MENU, {}, main_menu_message())
        return _transition(state, {}, cancel_message())
    return _transition(ConversationState.MENU, {}, main_menu_message())


async def _handle_menu(
    inbound: ConversationInput,
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    if action == MENU_BOOK:
        try:
            port = _require_booking_port(booking_port)
            services = _snapshot_options(
                await port.list_services(inbound.business_id)
            )
        except BookingPortUnavailable:
            return _transition(
                ConversationState.MENU,
                {},
                booking_unavailable_message(),
            )
        if not services:
            return _transition(ConversationState.MENU, {}, no_services_message())
        return _transition(
            ConversationState.BOOKING_SERVICE,
            {},
            service_selection_message(services),
        )
    if action == MENU_RESCHEDULE:
        return _transition(
            ConversationState.RESCHEDULE,
            {},
            reschedule_message(),
        )
    if action == MENU_CANCEL:
        return _transition(ConversationState.CANCEL, {}, cancel_message())
    if action == MENU_HUMAN:
        return _transition(
            ConversationState.HUMAN_HANDOFF,
            {},
            handoff_message(),
            automation_enabled=False,
            handoff_status="waiting",
        )
    return _transition(ConversationState.MENU, {}, main_menu_message())


async def _handle_service(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        services = _snapshot_options(await port.list_services(inbound.business_id))
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_SERVICE,
            context,
            booking_unavailable_message(),
        )
    if not services:
        return _transition(ConversationState.MENU, {}, no_services_message())

    service_id = _service_id(action)
    if service_id is None or not _option_exists(services, str(service_id)):
        if inbound.body and inbound.body.strip():
            return _handoff_transition()
        return _transition(
            ConversationState.BOOKING_SERVICE,
            context,
            service_selection_message(services),
        )

    try:
        intake = await port.get_service_intake(inbound.business_id, service_id)
    except BookingRequiresHandoff:
        return _handoff_transition()
    if (
        not intake.automatic_booking
        or intake.pricing_type is PricingType.HUMAN_QUOTE
    ):
        return _handoff_transition()
    return await _advance_intake(
        inbound,
        port,
        intake,
        {"service_id": str(service_id)},
        services=services,
    )


async def _handle_quantity(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_QUANTITY,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    quantity = _quantity(action, inbound.body)
    if quantity is None:
        return _transition(
            ConversationState.BOOKING_QUANTITY,
            context,
            quantity_selection_message(),
        )
    return await _advance_intake(
        inbound,
        port,
        intake,
        {**context, "service_id": str(service_id), "quantity": quantity},
    )


async def _handle_access(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_ACCESS,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    access = {
        ACCESS_NORMAL: AccessCondition.NORMAL,
        ACCESS_DIFFICULT: AccessCondition.DIFFICULT,
        ACCESS_UNKNOWN: AccessCondition.UNKNOWN,
    }.get(action)
    if access is None:
        return _transition(
            ConversationState.BOOKING_ACCESS,
            context,
            access_selection_message(),
        )
    return await _advance_intake(
        inbound,
        port,
        intake,
        {
            **context,
            "service_id": str(service_id),
            "access_condition": access.value,
        },
    )


async def _handle_address(
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    value = (inbound.body or "").strip()
    if value.casefold() in {"não sei", "nao sei"}:
        return _handoff_transition()
    if len(value) < 5 or len(value) > 500:
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            address_request_message(),
        )
    address = ServiceAddress(address_line=value)
    return await _advance_intake(
        inbound,
        port,
        intake,
        {
            **context,
            "service_id": str(service_id),
            "service_address": address.to_snapshot(),
        },
    )


async def _handle_site_limit(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_SITE_LIMIT,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    site_limit = _site_limit(action, inbound.body)
    if site_limit is False:
        return _transition(
            ConversationState.BOOKING_SITE_LIMIT,
            context,
            site_limit_message(),
        )
    updated = {
        **context,
        "service_id": str(service_id),
        "site_limit_answered": True,
    }
    if isinstance(site_limit, time):
        updated["site_allowed_end"] = site_limit.strftime("%H:%M")
    else:
        updated.pop("site_allowed_end", None)
    return await _advance_intake(inbound, port, intake, updated)


async def _handle_date(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_DATE,
            context,
            booking_unavailable_message(),
        )

    service_id = _context_service_id(context)
    if service_id is None:
        return await _restart_service_selection(inbound, port)
    requirements = _requirements_from_context(context)
    try:
        dates = _snapshot_options(
            await port.list_dates(
                inbound.business_id, service_id, requirements
            )
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not dates:
        return await _restart_service_selection(
            inbound,
            port,
            body="Não há datas disponíveis. Escolha outro serviço.",
        )

    selected_date = _selected_date(action)
    if selected_date is None or not _option_exists(dates, selected_date):
        return _transition(
            ConversationState.BOOKING_DATE,
            context,
            date_selection_message(dates),
        )

    try:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not times:
        return _transition(
            ConversationState.BOOKING_DATE,
            context,
            date_selection_message(
                dates,
                body="Não há horários nessa data. Escolha outra data.",
            ),
        )
    return _transition(
        ConversationState.BOOKING_TIME,
        {**_intake_context(context), "selected_date": selected_date},
        time_selection_message(times),
    )


async def _handle_time(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_TIME,
            context,
            booking_unavailable_message(),
        )

    service_id = _context_service_id(context)
    selected_date = _context_string(context, "selected_date")
    if service_id is None or selected_date is None:
        return await _restart_service_selection(inbound, port)
    requirements = _requirements_from_context(context)
    try:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not times:
        return await _return_to_dates(
            inbound,
            port,
            service_id,
            context,
            requirements,
        )

    selected_time = _selected_time(action)
    if selected_time is None or not _option_exists(times, selected_time):
        return _transition(
            ConversationState.BOOKING_TIME,
            context,
            time_selection_message(times),
        )

    candidate = {
        "service_id": str(service_id),
        "selected_date": selected_date,
        "selected_time": selected_time,
    }
    return _transition(
        ConversationState.BOOKING_CONFIRM,
        {**_intake_context(context), **candidate, "candidate_booking": candidate},
        booking_confirmation_message(),
    )


async def _handle_confirmation(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    if action == BOOKING_CANCEL:
        return _transition(
            ConversationState.MENU,
            {},
            booking_cancelled_message(),
        )
    if action not in {BOOKING_CONFIRM, BOOKING_BACK}:
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            booking_confirmation_message(),
        )
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            booking_unavailable_message(),
        )

    booking_data = _booking_data(context)
    if booking_data is None:
        return await _restart_service_selection(inbound, port)
    service_id, selected_date, selected_time = booking_data
    requirements = _requirements_from_context(context)

    if action == BOOKING_BACK:
        try:
            times = _snapshot_options(
                await port.list_times(
                    inbound.business_id,
                    service_id,
                    selected_date,
                    requirements,
                )
            )
        except BookingRequiresHandoff:
            return _handoff_transition()
        if not times:
            return _transition(
                ConversationState.BOOKING_CONFIRM,
                context,
                slot_unavailable_message(),
            )
        return _transition(
            ConversationState.BOOKING_TIME,
            _booking_time_context(context),
            time_selection_message(times),
        )

    try:
        confirmation = await port.confirm(
            inbound.business_id,
            inbound.customer_id,
            service_id,
            selected_date,
            selected_time,
            replace(
                requirements,
                idempotency_key=_booking_idempotency_key(inbound),
            ),
        )
    except SlotUnavailable:
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            slot_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not isinstance(confirmation, BookingConfirmation):
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            booking_unavailable_message(),
        )
    return _transition(
        ConversationState.COMPLETED,
        {},
        booking_completed_message(),
    )


async def _advance_intake(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    intake: ServiceIntake,
    context: dict[str, Any],
    *,
    services: Sequence[BookingOption] | None = None,
) -> ConversationTransition:
    if intake.requires_quantity and not isinstance(context.get("quantity"), int):
        return _transition(
            ConversationState.BOOKING_QUANTITY,
            context,
            quantity_selection_message(),
        )
    if (
        intake.considers_difficult_access
        and _context_string(context, "access_condition") is None
    ):
        return _transition(
            ConversationState.BOOKING_ACCESS,
            context,
            access_selection_message(),
        )
    if (
        intake.requires_address
        and ServiceAddress.from_snapshot(context.get("service_address")) is None
    ):
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            address_request_message(),
        )
    if intake.asks_site_time_limit and context.get("site_limit_answered") is not True:
        return _transition(
            ConversationState.BOOKING_SITE_LIMIT,
            context,
            site_limit_message(),
        )
    return await _offer_dates(inbound, port, context, services=services)


async def _offer_dates(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    *,
    services: Sequence[BookingOption] | None = None,
) -> ConversationTransition:
    service_id = _context_service_id(context)
    if service_id is None:
        return await _restart_service_selection(inbound, port)
    requirements = _requirements_from_context(context)
    try:
        plan = await port.estimate(
            inbound.business_id,
            service_id,
            requirements,
        )
        if plan.requires_handoff:
            return _handoff_transition()
        dates = _snapshot_options(
            await port.list_dates(
                inbound.business_id,
                service_id,
                requirements,
            )
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not dates:
        available_services = services or _snapshot_options(
            await port.list_services(inbound.business_id)
        )
        return _transition(
            ConversationState.BOOKING_SERVICE,
            {},
            service_selection_message(
                available_services,
                body="Não encontrei uma data disponível. Escolha outro serviço.",
            ),
        )
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(dates, body=_estimate_message(plan)),
    )


async def _context_intake(
    inbound: ConversationInput,
    context: dict[str, Any],
    port: BookingAvailabilityPort,
) -> tuple[uuid.UUID, ServiceIntake]:
    service_id = _context_service_id(context)
    if service_id is None:
        raise BookingRequiresHandoff("Service context is missing")
    intake = await port.get_service_intake(inbound.business_id, service_id)
    return service_id, intake


async def _restart_service_selection(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    *,
    body: str = "Escolha um serviço para continuar.",
) -> ConversationTransition:
    services = _snapshot_options(await port.list_services(inbound.business_id))
    if not services:
        return _transition(ConversationState.MENU, {}, no_services_message())
    return _transition(
        ConversationState.BOOKING_SERVICE,
        {},
        service_selection_message(services, body=body),
    )


async def _return_to_dates(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    service_id: uuid.UUID,
    context: dict[str, Any],
    requirements: BookingRequirements,
) -> ConversationTransition:
    dates = _snapshot_options(
        await port.list_dates(inbound.business_id, service_id, requirements)
    )
    if not dates:
        return await _restart_service_selection(inbound, port)
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(
            dates,
            body="Não há horários disponíveis. Escolha outra data.",
        ),
    )


def _transition(
    state: ConversationState,
    context: dict[str, Any],
    outbound: OutboundMessage,
    *,
    automation_enabled: bool = True,
    handoff_status: str = "none",
) -> ConversationTransition:
    return ConversationTransition(
        state=state,
        context=context,
        automation_enabled=automation_enabled,
        handoff_status=handoff_status,
        outbound=outbound,
    )


def _require_booking_port(
    booking_port: BookingAvailabilityPort | None,
) -> BookingAvailabilityPort:
    if booking_port is None:
        raise BookingPortUnavailable("Booking availability adapter is unavailable")
    return booking_port


def _snapshot_options(
    options: Sequence[BookingOption],
) -> tuple[BookingOption, ...]:
    return tuple(BookingOption(option.id, option.label) for option in options)


def _option_exists(options: Sequence[BookingOption], expected_id: str) -> bool:
    return any(option.id == expected_id for option in options)


def _canonical_state(value: str) -> ConversationState:
    if value == "new":
        return ConversationState.START
    try:
        return ConversationState(value)
    except ValueError:
        return ConversationState.START


def _clean_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if key in ALLOWED_CONTEXT_KEYS
    }


def _service_id(action: str | None) -> uuid.UUID | None:
    if action is None or not action.startswith("service:"):
        return None
    try:
        return uuid.UUID(action.removeprefix("service:"))
    except ValueError:
        return None


def _selected_date(action: str | None) -> str | None:
    if action is None or not action.startswith("date:"):
        return None
    raw_date = action.removeprefix("date:")
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError:
        return None
    return raw_date if parsed_date.isoformat() == raw_date else None


def _selected_time(action: str | None) -> str | None:
    if action is None or not action.startswith("time:"):
        return None
    value = action.removeprefix("time:")
    if (
        not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _context_service_id(context: dict[str, Any]) -> uuid.UUID | None:
    value = _context_string(context, "service_id")
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _context_string(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    return value if isinstance(value, str) and value else None


def _booking_data(
    context: dict[str, Any],
) -> tuple[uuid.UUID, str, str] | None:
    service_id = _context_service_id(context)
    selected_date = _context_string(context, "selected_date")
    selected_time = _context_string(context, "selected_time")
    if service_id is None or selected_date is None or selected_time is None:
        return None
    return service_id, selected_date, selected_time


def _booking_time_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if key not in {"selected_time", "candidate_booking"}
    }


def _intake_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in context.items()
        if key
        in {
            "service_id",
            "quantity",
            "access_condition",
            "service_address",
            "site_allowed_end",
            "site_limit_answered",
        }
    }


def _requirements_from_context(context: dict[str, Any]) -> BookingRequirements:
    quantity = context.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        quantity = 1
    try:
        access = AccessCondition(
            _context_string(context, "access_condition")
            or AccessCondition.NORMAL.value
        )
    except ValueError:
        access = AccessCondition.UNKNOWN
    address = ServiceAddress.from_snapshot(context.get("service_address"))
    site_limit_value = _context_string(context, "site_allowed_end")
    site_limit: time | None = None
    if site_limit_value is not None:
        try:
            parsed = time.fromisoformat(site_limit_value)
            if parsed.strftime("%H:%M") == site_limit_value:
                site_limit = parsed
        except ValueError:
            pass
    return BookingRequirements(
        quantity=quantity,
        access_condition=access,
        address=address,
        site_allowed_end=site_limit,
    )


def _quantity(action: str | None, body: str | None) -> int | None:
    raw_value = ""
    if action and action.startswith("quantity:"):
        raw_value = action.removeprefix("quantity:")
    elif body:
        raw_value = body.strip()
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if 1 <= value <= 999 else None


def _site_limit(
    action: str | None,
    body: str | None,
) -> time | None | bool:
    if action == SITE_LIMIT_NONE:
        return None
    mapped = {SITE_LIMIT_17: "17:00", SITE_LIMIT_18: "18:00"}.get(action)
    raw_value = mapped or (body or "").strip()
    try:
        parsed = time.fromisoformat(raw_value)
    except ValueError:
        return False
    if parsed.tzinfo is not None or parsed.strftime("%H:%M") != raw_value:
        return False
    return parsed


def _estimate_message(plan: BookingPlan) -> str:
    price = plan.service.estimated_price
    if price is None:
        return "Escolha uma data para o atendimento."
    formatted = _format_brl(price)
    if plan.service.pricing_type is PricingType.FIXED:
        return f"O valor do serviço é {formatted}. Escolha uma data."
    return (
        f"Pelas informações que você passou, o valor estimado é {formatted}. "
        "Ele pode mudar se houver uma condição diferente no local. "
        "Escolha uma data."
    )


def _format_brl(value: Decimal) -> str:
    normalized = f"{value:,.2f}"
    return f"R$ {normalized.replace(',', '#').replace('.', ',').replace('#', '.')}"


def _booking_idempotency_key(inbound: ConversationInput) -> str:
    stable = "\x1f".join(
        (
            str(inbound.business_id),
            str(inbound.customer_id),
            inbound.provider_message_id,
        )
    )
    return f"booking:confirm:{hashlib.sha256(stable.encode()).hexdigest()}"


def _handoff_transition() -> ConversationTransition:
    return _transition(
        ConversationState.HUMAN_HANDOFF,
        {},
        handoff_message(),
        automation_enabled=False,
        handoff_status="waiting",
    )

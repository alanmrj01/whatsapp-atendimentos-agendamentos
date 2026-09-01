from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.conversations.constants import (
    ALLOWED_CONTEXT_KEYS,
    BOOKING_BACK,
    BOOKING_CANCEL,
    BOOKING_CONFIRM,
    MENU_BOOK,
    MENU_CANCEL,
    MENU_HUMAN,
    MENU_RESCHEDULE,
    ConversationState,
)
from app.conversations.outbound import (
    OutboundMessage,
    booking_cancelled_message,
    booking_completed_message,
    booking_confirmation_message,
    booking_unavailable_message,
    cancel_message,
    date_selection_message,
    handoff_message,
    main_menu_message,
    no_services_message,
    reschedule_message,
    service_selection_message,
    slot_unavailable_message,
    time_selection_message,
)
from app.conversations.ports import (
    BookingAvailabilityPort,
    BookingConfirmation,
    BookingOption,
    BookingPortUnavailable,
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
        return _transition(
            ConversationState.BOOKING_SERVICE,
            context,
            service_selection_message(services),
        )

    dates = _snapshot_options(
        await port.list_dates(inbound.business_id, service_id)
    )
    if not dates:
        return _transition(
            ConversationState.BOOKING_SERVICE,
            context,
            service_selection_message(
                services,
                body="Esse serviço não possui datas disponíveis. Escolha outro.",
            ),
        )
    return _transition(
        ConversationState.BOOKING_DATE,
        {"service_id": str(service_id)},
        date_selection_message(dates),
    )


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
    dates = _snapshot_options(
        await port.list_dates(inbound.business_id, service_id)
    )
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

    times = _snapshot_options(
        await port.list_times(
            inbound.business_id,
            service_id,
            selected_date,
        )
    )
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
        {
            "service_id": str(service_id),
            "selected_date": selected_date,
        },
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
    times = _snapshot_options(
        await port.list_times(
            inbound.business_id,
            service_id,
            selected_date,
        )
    )
    if not times:
        return await _return_to_dates(inbound, port, service_id)

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
        {**candidate, "candidate_booking": candidate},
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

    if action == BOOKING_BACK:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
            )
        )
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
        )
    except SlotUnavailable:
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            slot_unavailable_message(),
        )
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
) -> ConversationTransition:
    dates = _snapshot_options(
        await port.list_dates(inbound.business_id, service_id)
    )
    if not dates:
        return await _restart_service_selection(inbound, port)
    return _transition(
        ConversationState.BOOKING_DATE,
        {"service_id": str(service_id)},
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
        if key in {"service_id", "selected_date"}
    }

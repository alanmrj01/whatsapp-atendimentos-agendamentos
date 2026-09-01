from __future__ import annotations

import uuid
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
    cancel_message,
    date_selection_message,
    handoff_message,
    main_menu_message,
    reschedule_message,
    service_selection_message,
    time_selection_message,
)
from app.conversations.ports import BookingAvailabilityPort
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
        if action == MENU_BOOK:
            if booking_port is not None:
                await booking_port.list_services(inbound.business_id)
            return _transition(
                ConversationState.BOOKING_SERVICE,
                {},
                service_selection_message(),
            )
        if action == MENU_RESCHEDULE:
            return _transition(
                ConversationState.RESCHEDULE,
                {},
                reschedule_message(),
            )
        if action == MENU_CANCEL:
            return _transition(
                ConversationState.CANCEL,
                {},
                cancel_message(),
            )
        if action == MENU_HUMAN:
            return _transition(
                ConversationState.HUMAN_HANDOFF,
                {},
                handoff_message(),
                automation_enabled=False,
                handoff_status="waiting",
            )
        return _transition(ConversationState.MENU, {}, main_menu_message())

    if state is ConversationState.BOOKING_SERVICE:
        service_id = _service_id(action)
        if service_id is None or not await _service_exists(
            booking_port,
            inbound.business_id,
            service_id,
        ):
            return _transition(state, context, service_selection_message())
        if booking_port is not None:
            await booking_port.list_dates(inbound.business_id, service_id)
        return _transition(
            ConversationState.BOOKING_DATE,
            {"service_id": str(service_id)},
            date_selection_message(),
        )

    if state is ConversationState.BOOKING_DATE:
        service_id = _context_service_id(context)
        selected_date = _selected_date(action)
        if (
            service_id is None
            or selected_date is None
            or not await _date_exists(
                booking_port,
                inbound.business_id,
                service_id,
                selected_date,
            )
        ):
            return _transition(state, context, date_selection_message())
        if booking_port is not None:
            await booking_port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
            )
        return _transition(
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(service_id),
                "selected_date": selected_date,
            },
            time_selection_message(),
        )

    if state is ConversationState.BOOKING_TIME:
        service_id = _context_service_id(context)
        selected_date = _context_string(context, "selected_date")
        selected_time = _selected_time(action)
        if (
            service_id is None
            or selected_date is None
            or selected_time is None
            or not await _time_exists(
                booking_port,
                inbound.business_id,
                service_id,
                selected_date,
                selected_time,
            )
        ):
            return _transition(state, context, time_selection_message())
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

    if state is ConversationState.BOOKING_CONFIRM:
        if action == BOOKING_BACK:
            booking_data = _booking_data(context)
            if booking_port is not None and booking_data is not None:
                service_id, selected_date, _ = booking_data
                await booking_port.list_times(
                    inbound.business_id,
                    service_id,
                    selected_date,
                )
            return _transition(
                ConversationState.BOOKING_TIME,
                _booking_time_context(context),
                time_selection_message(),
            )
        if action == BOOKING_CANCEL:
            return _transition(
                ConversationState.MENU,
                {},
                booking_cancelled_message(),
            )
        if action != BOOKING_CONFIRM:
            return _transition(state, context, booking_confirmation_message())

        booking_data = _booking_data(context)
        if booking_data is None:
            return _transition(
                ConversationState.BOOKING_SERVICE,
                {},
                service_selection_message(),
            )
        service_id, selected_date, selected_time = booking_data
        if booking_port is not None:
            await booking_port.confirm(
                inbound.business_id,
                inbound.customer_id,
                service_id,
                selected_date,
                selected_time,
            )
        return _transition(
            ConversationState.COMPLETED,
            {},
            booking_completed_message(),
        )

    if state is ConversationState.RESCHEDULE:
        if action == BOOKING_BACK:
            return _transition(ConversationState.MENU, {}, main_menu_message())
        return _transition(state, {}, reschedule_message())

    if state is ConversationState.CANCEL:
        if action == BOOKING_BACK:
            return _transition(ConversationState.MENU, {}, main_menu_message())
        return _transition(state, {}, cancel_message())

    return _transition(ConversationState.MENU, {}, main_menu_message())


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


async def _service_exists(
    booking_port: BookingAvailabilityPort | None,
    business_id: uuid.UUID,
    service_id: uuid.UUID,
) -> bool:
    if booking_port is None:
        return True
    options = await booking_port.list_services(business_id)
    return any(option.id == str(service_id) for option in options)


async def _date_exists(
    booking_port: BookingAvailabilityPort | None,
    business_id: uuid.UUID,
    service_id: uuid.UUID,
    selected_date: str,
) -> bool:
    if booking_port is None:
        return True
    options = await booking_port.list_dates(business_id, service_id)
    return any(option.id == selected_date for option in options)


async def _time_exists(
    booking_port: BookingAvailabilityPort | None,
    business_id: uuid.UUID,
    service_id: uuid.UUID,
    selected_date: str,
    selected_time: str,
) -> bool:
    if booking_port is None:
        return True
    options = await booking_port.list_times(
        business_id,
        service_id,
        selected_date,
    )
    return any(option.id == selected_time for option in options)

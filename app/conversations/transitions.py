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
    AddressResolutionStatus,
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
    CANCEL_ABORT,
    CANCEL_CONFIRM,
    RESCHEDULE_CONFIRM,
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
    cancel_completed_message,
    cancel_confirmation_message,
    cancel_message,
    date_selection_message,
    existing_booking_selection_message,
    handoff_message,
    main_menu_message,
    no_services_message,
    quantity_selection_message,
    reschedule_completed_message,
    reschedule_confirmation_message,
    reschedule_message,
    service_selection_message,
    site_limit_message,
    slot_unavailable_message,
    tubing_length_message,
    time_selection_message,
)
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
    Interpretation,
    normalize_portuguese,
)
from app.conversations.service_semantics import semantic_service_score
from app.conversations.ports import (
    BookingAvailabilityPort,
    BookingConfirmation,
    BookingNotFound,
    BookingOption,
    BookingPortUnavailable,
    BookingRecoveryRequired,
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
    if not conversation.assistant_enabled or not conversation.automation_enabled:
        return None

    state = _canonical_state(conversation.state)
    context = _clean_context(conversation.context)
    action = inbound.interactive_id
    interpretation = DeterministicConversationInterpreter().interpret(inbound.body)

    if interpretation.intent is ConversationIntent.HUMAN_HANDOFF:
        return _handoff_transition(conversation.handoff_message)

    if state in {
        ConversationState.START,
        ConversationState.COMPLETED,
        ConversationState.HUMAN_HANDOFF,
    }:
        return await _handle_natural_start(
            conversation,
            inbound,
            interpretation,
            booking_port,
        )
    if state is ConversationState.MENU:
        return await _handle_menu(
            inbound,
            action,
            booking_port,
            interpretation=interpretation,
            greeting_message=conversation.greeting_message,
            fallback_message=conversation.fallback_message,
            handoff_message=conversation.handoff_message,
        )
    if state is ConversationState.BOOKING_SERVICE:
        return await _handle_service(
            inbound,
            context,
            action,
            booking_port,
            interpretation=interpretation,
            fallback_message=conversation.fallback_message,
        )
    if state is ConversationState.BOOKING_QUANTITY:
        return await _handle_quantity(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_ACCESS:
        return await _handle_access(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_ADDRESS:
        return await _handle_address(inbound, context, booking_port)
    if state is ConversationState.BOOKING_TUBING:
        return await _handle_tubing(inbound, context, booking_port)
    if state is ConversationState.BOOKING_SITE_LIMIT:
        return await _handle_site_limit(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_DATE:
        return await _handle_date(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_TIME:
        return await _handle_time(inbound, context, action, booking_port)
    if state is ConversationState.BOOKING_CONFIRM:
        return await _handle_confirmation(inbound, context, action, booking_port)
    if state is ConversationState.RESCHEDULE:
        return await _handle_reschedule(inbound, context, action, booking_port)
    if state is ConversationState.CANCEL:
        return await _handle_cancel(inbound, context, action, booking_port)
    return _transition(ConversationState.MENU, {}, main_menu_message())


async def _handle_natural_start(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    if interpretation.intent is ConversationIntent.GREETING:
        return _transition(
            ConversationState.MENU,
            {},
            _text_message(conversation.greeting_message),
        )
    if interpretation.intent is ConversationIntent.RESCHEDULE:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="reschedule")
    if interpretation.intent is ConversationIntent.CANCEL:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="cancel")
    if interpretation.intent is ConversationIntent.HUMAN_HANDOFF:
        return _handoff_transition(conversation.handoff_message)
    if interpretation.intent in {
        ConversationIntent.BOOK,
        ConversationIntent.AVAILABILITY,
        ConversationIntent.SERVICE_INTENT,
    }:
        try:
            port = _require_booking_port(booking_port)
            services = _snapshot_options(
                await port.list_services(inbound.business_id)
            )
        except BookingPortUnavailable:
            return _transition(
                ConversationState.MENU, {}, booking_unavailable_message()
            )
        if not services:
            return _transition(ConversationState.MENU, {}, no_services_message())
        matched = _service_for_interpretation(services, interpretation)
        if matched is not None:
            return await _handle_service(
                inbound,
                {},
                f"service:{matched.id}",
                port,
                interpretation=interpretation,
                fallback_message=conversation.fallback_message,
            )
        return _transition(
            ConversationState.BOOKING_SERVICE,
            {},
            service_selection_message(
                services, body="Qual serviço você precisa?"
            ),
        )
    return _transition(
        ConversationState.MENU,
        {},
        _text_message(conversation.fallback_message),
    )


async def _handle_menu(
    inbound: ConversationInput,
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    interpretation: Interpretation | None = None,
    greeting_message: str = "Olá! Como posso ajudar com seu ar-condicionado?",
    fallback_message: str = "Não entendi. Conte em poucas palavras o serviço que você precisa.",
    handoff_message: str = "Seu atendimento foi encaminhado para uma pessoa da equipe.",
) -> ConversationTransition:
    interpretation = interpretation or DeterministicConversationInterpreter().interpret(
        inbound.body
    )
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
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="reschedule")
    if action == MENU_CANCEL:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="cancel")
    if action == MENU_HUMAN:
        return _transition(
            ConversationState.HUMAN_HANDOFF,
            {},
            _text_message(handoff_message),
            automation_enabled=False,
            handoff_status="waiting",
        )
    if action is None:
        snapshot = ConversationSnapshot(
            business_id=inbound.business_id,
            customer_id=inbound.customer_id,
            conversation_id=inbound.conversation_id,
            state=ConversationState.MENU.value,
            context={},
            automation_enabled=True,
            handoff_status="none",
            greeting_message=greeting_message,
            fallback_message=fallback_message,
            handoff_message=handoff_message,
        )
        return await _handle_natural_start(
            snapshot, inbound, interpretation, booking_port
        )
    return _transition(ConversationState.MENU, {}, main_menu_message())


async def _handle_service(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    interpretation: Interpretation | None = None,
    fallback_message: str = "Não entendi. Conte em poucas palavras o serviço que você precisa.",
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
    if service_id is None:
        interpretation = interpretation or DeterministicConversationInterpreter().interpret(
            inbound.body
        )
        matched = _service_for_interpretation(services, interpretation)
        service_id = uuid.UUID(matched.id) if matched is not None else None
    if service_id is None or not _option_exists(services, str(service_id)):
        if interpretation and interpretation.intent in {
            ConversationIntent.BOOK,
            ConversationIntent.AVAILABILITY,
        }:
            return _transition(
                ConversationState.BOOKING_SERVICE,
                context,
                service_selection_message(
                    services,
                    body="Claro. Qual serviço você quer agendar?",
                ),
            )
        if inbound.body and inbound.body.strip():
            return _transition(
                ConversationState.BOOKING_SERVICE,
                context,
                service_selection_message(services, body=fallback_message),
            )
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
    except BookingRecoveryRequired as exc:
        return await _recover_booking_issue(
            inbound,
            port,
            ConversationState.BOOKING_ADDRESS,
            context,
            exc,
        )
    except BookingRequiresHandoff:
        return _handoff_transition()

    value = (inbound.body or "").strip()
    normalized = normalize_portuguese(value)

    if context.get("address_confirmation_pending") is True:
        current_address = ServiceAddress.from_snapshot(
            context.get("service_address")
        )
        if normalized in {"sim", "correto", "isso", "confirmo", "certo"}:
            if current_address is None:
                return _transition(
                    ConversationState.BOOKING_ADDRESS,
                    _clear_address_recovery_context(context),
                    address_request_message(),
                )
            updated = _clear_address_recovery_context(context)
            updated["service_id"] = str(service_id)
            updated["service_address"] = current_address.to_snapshot()
            return await _advance_intake(
                inbound,
                port,
                intake,
                updated,
            )
        if normalized in {"nao", "não", "errado", "corrigir"}:
            updated = _clear_address_recovery_context(context)
            updated.pop("service_address", None)
            return _transition(
                ConversationState.BOOKING_ADDRESS,
                updated,
                address_request_message(),
            )
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            _text_message(
                "O endereço informado está correto? Responda apenas sim ou não."
            ),
        )

    missing_component = _context_string(
        context, "address_missing_component"
    )
    draft = context.get("address_draft")
    draft_raw = (
        draft.get("raw")
        if isinstance(draft, dict) and isinstance(draft.get("raw"), str)
        else None
    )

    if normalized in {"nao sei", "não sei"}:
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            _text_message(
                "Sem problema. Informe pelo menos a rua e o número do local. "
                "Se souber, inclua também o bairro."
            ),
        )

    raw_address = (
        _merge_address_followup(draft_raw, missing_component, value)
        if draft_raw and missing_component
        else value
    )
    if len(raw_address) < 5 or len(raw_address) > 500:
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            _address_missing_component_message(missing_component),
        )

    resolution = await port.resolve_service_address(
        inbound.business_id,
        raw_address,
    )
    if (
        resolution.status
        is AddressResolutionStatus.TEMPORARILY_UNAVAILABLE
    ):
        updated = dict(context)
        updated["address_draft"] = {"raw": raw_address}
        updated.pop("address_missing_component", None)
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            updated,
            _text_message(
                "Não consegui validar o endereço agora. "
                "Pode reenviar o endereço para eu tentar novamente?"
            ),
        )

    if (
        resolution.status is AddressResolutionStatus.NEEDS_INPUT
        or resolution.address is None
    ):
        component = resolution.missing_component or "generic"
        updated = dict(context)
        updated["service_id"] = str(service_id)
        updated["address_draft"] = {"raw": raw_address}
        updated["address_missing_component"] = component
        updated.pop("service_address", None)
        updated.pop("address_confirmation_pending", None)
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            updated,
            _address_missing_component_message(component),
        )

    updated = _clear_address_recovery_context(context)
    updated["service_id"] = str(service_id)
    updated["service_address"] = resolution.address.to_snapshot()

    if resolution.status is AddressResolutionStatus.NEEDS_CONFIRMATION:
        updated["address_confirmation_pending"] = True
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            updated,
            _text_message(
                "Só para confirmar: o endereço que você informou está correto? "
                "Responda sim ou não."
            ),
        )

    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
    )


async def _handle_tubing(
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_TUBING,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff:
        return _handoff_transition()

    normalized = normalize_portuguese(inbound.body or "")
    updated = {
        **context,
        "service_id": str(service_id),
        "tubing_length_answered": True,
    }
    if normalized in {"nao sei", "não sei", "nao tenho certeza", "não tenho certeza"}:
        updated.pop("tubing_meters", None)
        return await _advance_intake(inbound, port, intake, updated)

    meters = _decimal_from_text(inbound.body)
    if meters is None or meters <= 0 or meters > Decimal("100"):
        return _transition(
            ConversationState.BOOKING_TUBING,
            context,
            tubing_length_message(),
        )
    updated["tubing_meters"] = str(meters)
    return await _advance_intake(inbound, port, intake, updated)


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

    selected_date = _selected_date(action) or _date_from_text(inbound.body, dates)
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
    selected_time = _time_from_text(inbound.body, times)
    if selected_time is not None:
        candidate = {
            "service_id": str(service_id),
            "selected_date": selected_date,
            "selected_time": selected_time,
        }
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            {
                **_intake_context(context),
                **candidate,
                "candidate_booking": candidate,
            },
            booking_confirmation_message(),
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

    selected_time = _selected_time(action) or _time_from_text(inbound.body, times)
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
    if action is None:
        normalized = normalize_portuguese(inbound.body or "")
        if normalized in {"confirmar", "confirmo", "sim", "pode confirmar", "pode"}:
            action = BOOKING_CONFIRM
        elif normalized in {"voltar", "outro horario", "trocar horario"}:
            action = BOOKING_BACK
        elif normalized in {"cancelar", "cancela", "nao"}:
            action = BOOKING_CANCEL
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


async def _begin_existing_booking_flow(
    inbound: ConversationInput,
    booking_port: BookingAvailabilityPort | None,
    *,
    purpose: str,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        bookings = await port.list_customer_bookings(
            inbound.business_id,
            inbound.customer_id,
        )
    except BookingPortUnavailable:
        return _transition(ConversationState.MENU, {}, booking_unavailable_message())

    if not bookings:
        return _transition(
            ConversationState.MENU,
            {},
            reschedule_message() if purpose == "reschedule" else cancel_message(),
        )
    options = tuple(
        BookingOption(str(item.appointment_id), item.label)
        for item in bookings
    )
    return _transition(
        ConversationState.RESCHEDULE if purpose == "reschedule" else ConversationState.CANCEL,
        {},
        existing_booking_selection_message(options, purpose=purpose),
    )


async def _handle_reschedule(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(ConversationState.RESCHEDULE, context, booking_unavailable_message())

    if action == BOOKING_CANCEL:
        return _transition(ConversationState.MENU, {}, booking_cancelled_message())

    appointment_id = _context_uuid(context, "appointment_id")
    service_id = _context_service_id(context)
    selected_date = _context_string(context, "selected_date")
    selected_time = _context_string(context, "selected_time")

    if appointment_id is None:
        bookings = await port.list_customer_bookings(
            inbound.business_id,
            inbound.customer_id,
        )
        selected_id = _appointment_id(action)
        selected = next(
            (item for item in bookings if item.appointment_id == selected_id),
            None,
        )
        if selected is None:
            options = tuple(
                BookingOption(str(item.appointment_id), item.label)
                for item in bookings
            )
            if not options:
                return _transition(ConversationState.MENU, {}, reschedule_message())
            return _transition(
                ConversationState.RESCHEDULE,
                {},
                existing_booking_selection_message(options, purpose="reschedule"),
            )
        base_context = _context_from_existing_booking(selected)
        dates = _snapshot_options(
            await port.list_dates(
                inbound.business_id,
                selected.service_id,
                selected.requirements,
            )
        )
        if not dates:
            return _transition(
                ConversationState.RESCHEDULE,
                base_context,
                booking_unavailable_message(),
            )
        return _transition(
            ConversationState.RESCHEDULE,
            base_context,
            date_selection_message(
                dates,
                body="Escolha a nova data do atendimento.",
            ),
        )

    requirements = _requirements_from_context(context)
    if service_id is None:
        return await _begin_existing_booking_flow(
            inbound, port, purpose="reschedule"
        )

    if selected_date is None:
        dates = _snapshot_options(
            await port.list_dates(inbound.business_id, service_id, requirements)
        )
        selected_date = _selected_date(action) or _date_from_text(inbound.body, dates)
        if selected_date is None or not _option_exists(dates, selected_date):
            return _transition(
                ConversationState.RESCHEDULE,
                context,
                date_selection_message(dates, body="Escolha a nova data do atendimento."),
            )
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
        return _transition(
            ConversationState.RESCHEDULE,
            {**context, "selected_date": selected_date},
            time_selection_message(times, body="Escolha o novo horário."),
        )

    if selected_time is None:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
        selected_time = _selected_time(action) or _time_from_text(inbound.body, times)
        if selected_time is None or not _option_exists(times, selected_time):
            return _transition(
                ConversationState.RESCHEDULE,
                context,
                time_selection_message(times, body="Escolha o novo horário."),
            )
        return _transition(
            ConversationState.RESCHEDULE,
            {**context, "selected_time": selected_time},
            reschedule_confirmation_message(),
        )

    normalized = normalize_portuguese(inbound.body or "")
    if action is None and normalized in {"confirmar", "confirmo", "sim", "pode", "pode confirmar"}:
        action = RESCHEDULE_CONFIRM
    elif action is None and normalized in {"voltar", "outro horario", "trocar horario"}:
        action = BOOKING_BACK

    if action == BOOKING_BACK:
        updated = dict(context)
        updated.pop("selected_time", None)
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
        return _transition(
            ConversationState.RESCHEDULE,
            updated,
            time_selection_message(times, body="Escolha o novo horário."),
        )
    if action != RESCHEDULE_CONFIRM:
        return _transition(
            ConversationState.RESCHEDULE,
            context,
            reschedule_confirmation_message(),
        )

    try:
        result = await port.reschedule_booking_atomic(
            inbound.business_id,
            inbound.customer_id,
            appointment_id,
            selected_date,
            selected_time,
            requirements,
        )
    except (BookingNotFound, SlotUnavailable):
        return await _begin_existing_booking_flow(
            inbound, port, purpose="reschedule"
        )
    except BookingRequiresHandoff:
        return _handoff_transition()
    if not isinstance(result, BookingConfirmation):
        return _transition(
            ConversationState.RESCHEDULE,
            context,
            booking_unavailable_message(),
        )
    return _transition(
        ConversationState.COMPLETED,
        {},
        reschedule_completed_message(),
    )


async def _handle_cancel(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(ConversationState.CANCEL, context, booking_unavailable_message())

    appointment_id = _context_uuid(context, "appointment_id")
    if appointment_id is None:
        bookings = await port.list_customer_bookings(
            inbound.business_id,
            inbound.customer_id,
        )
        selected_id = _appointment_id(action)
        selected = next(
            (item for item in bookings if item.appointment_id == selected_id),
            None,
        )
        if selected is None:
            options = tuple(
                BookingOption(str(item.appointment_id), item.label)
                for item in bookings
            )
            if not options:
                return _transition(ConversationState.MENU, {}, cancel_message())
            return _transition(
                ConversationState.CANCEL,
                {},
                existing_booking_selection_message(options, purpose="cancel"),
            )
        return _transition(
            ConversationState.CANCEL,
            {"appointment_id": str(selected.appointment_id)},
            cancel_confirmation_message(),
        )

    normalized = normalize_portuguese(inbound.body or "")
    if action is None and normalized in {"sim", "confirmar", "cancela", "cancelar"}:
        action = CANCEL_CONFIRM
    elif action is None and normalized in {"nao", "não", "manter", "voltar"}:
        action = CANCEL_ABORT

    if action == CANCEL_ABORT or action == BOOKING_BACK:
        return _transition(ConversationState.MENU, {}, main_menu_message())
    if action != CANCEL_CONFIRM:
        return _transition(
            ConversationState.CANCEL,
            context,
            cancel_confirmation_message(),
        )
    try:
        result = await port.cancel_booking(
            inbound.business_id,
            inbound.customer_id,
            appointment_id,
        )
    except BookingNotFound:
        return await _begin_existing_booking_flow(
            inbound, port, purpose="cancel"
        )
    if not isinstance(result, BookingConfirmation):
        return _transition(
            ConversationState.CANCEL,
            context,
            booking_unavailable_message(),
        )
    return _transition(
        ConversationState.COMPLETED,
        {},
        cancel_completed_message(),
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
    if intake.asks_tubing_length and context.get("tubing_length_answered") is not True:
        return _transition(
            ConversationState.BOOKING_TUBING,
            context,
            tubing_length_message(),
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
    available_dates: list[BookingOption] = []
    availability_lines: list[str] = []
    for option in dates:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                option.id,
                requirements,
            )
        )
        if not times:
            continue
        available_dates.append(option)
        labels = ", ".join(item.label for item in times[:3])
        availability_lines.append(f"{option.label} — {labels}")
        if len(available_dates) == 3:
            break
    if not available_dates:
        return await _restart_service_selection(
            inbound,
            port,
            body="Não encontrei horários compatíveis. Escolha outro serviço.",
        )
    body = _estimate_message(plan) + "\n\nTenho disponibilidade:\n"
    body += "\n".join(availability_lines)
    body += "\n\nQual fica melhor?"
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(tuple(available_dates), body=body),
    )


async def _context_intake(
    inbound: ConversationInput,
    context: dict[str, Any],
    port: BookingAvailabilityPort,
) -> tuple[uuid.UUID, ServiceIntake]:
    service_id = _context_service_id(context)
    if service_id is None:
        raise BookingRecoveryRequired("service_context_missing")
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


async def _recover_booking_issue(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    state: ConversationState,
    context: dict[str, Any],
    error: BookingRecoveryRequired,
) -> ConversationTransition:
    if error.reason in {
        "service_context_missing",
        "service_unavailable",
        "service_configuration_invalid",
        "no_active_technician",
    }:
        return await _restart_service_selection(
            inbound,
            port,
            body=(
                "Não consegui continuar com esse serviço automaticamente. "
                "Escolha o serviço que melhor descreve o que você precisa."
            ),
        )

    if error.reason in {
        "travel_estimate_unavailable",
        "route_temporarily_unavailable",
        "route_not_found",
        "route_invalid_response",
        "origin_address_unavailable",
        "destination_address_unavailable",
        "origin_configuration_unavailable",
        "address_outside_service_area",
    }:
        address = ServiceAddress.from_snapshot(context.get("service_address"))
        if address is not None:
            updated = _intake_context(context)
            updated["service_address"] = address.to_snapshot()
            updated["address_confirmation_pending"] = True
            return _transition(
                ConversationState.BOOKING_ADDRESS,
                updated,
                _text_message(
                    "Não consegui concluir o cálculo do deslocamento. "
                    "O endereço informado está correto? Responda sim ou não."
                ),
            )
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            _intake_context(context),
            address_request_message(),
        )

    return _transition(
        state,
        context,
        booking_unavailable_message(),
    )


def _clear_address_recovery_context(
    context: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(context)
    updated.pop("address_draft", None)
    updated.pop("address_missing_component", None)
    updated.pop("address_confirmation_pending", None)
    return updated


def _address_missing_component_message(
    component: str | None,
) -> OutboundMessage:
    messages = {
        "street_number": "Qual é o número do endereço?",
        "route": "Qual é o nome da rua ou avenida?",
        "locality": "Em qual cidade fica o endereço?",
        "administrative_area": "Em qual estado fica o endereço?",
        "postal_code": "Qual é o CEP do endereço?",
    }
    return _text_message(
        messages.get(
            component or "generic",
            "Preciso de mais um detalhe do endereço. "
            "Envie rua, número e bairro do local.",
        )
    )


def _merge_address_followup(
    draft_raw: str,
    component: str,
    reply: str,
) -> str:
    clean_reply = reply.strip()
    if component == "street_number":
        return f"{draft_raw}, número {clean_reply}"
    if component == "route":
        return f"{clean_reply}, {draft_raw}"
    if component == "locality":
        return f"{draft_raw}, {clean_reply}"
    if component == "administrative_area":
        return f"{draft_raw}, {clean_reply}"
    if component == "postal_code":
        return f"{draft_raw}, CEP {clean_reply}"
    return f"{draft_raw}, {clean_reply}"


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
    return tuple(
        BookingOption(option.id, option.label, tuple(option.examples))
        for option in options
    )


def _option_exists(options: Sequence[BookingOption], expected_id: str) -> bool:
    return any(option.id == expected_id for option in options)


def _service_for_interpretation(
    services: Sequence[BookingOption],
    interpretation: Interpretation,
) -> BookingOption | None:
    text = interpretation.normalized_text
    if not text:
        return None

    ranked = sorted(
        (
            (
                semantic_service_score(text, service.label, service.examples),
                service,
            )
            for service in services
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.48:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None
    return ranked[0][1]


def _date_from_text(
    body: str | None,
    dates: Sequence[BookingOption],
) -> str | None:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return None
    for option in dates:
        label = normalize_portuguese(option.label)
        day_month = date.fromisoformat(option.id).strftime("%d/%m")
        weekday = label.split(" ", 1)[0]
        if label in normalized or day_month in normalized or weekday in normalized:
            return option.id
    return None


def _time_from_text(
    body: str | None,
    times: Sequence[BookingOption],
) -> str | None:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return None
    tokens = set(normalized.split())
    for option in times:
        hour, minute = option.label.split(":", 1)
        candidates = {option.label, str(int(hour)), f"{int(hour)}h"}
        if minute != "00":
            candidates.add(f"{int(hour)} {minute}")
        if any(candidate in normalized or candidate in tokens for candidate in candidates):
            return option.id
    return None


def _text_message(body: str) -> OutboundMessage:
    return OutboundMessage(message_type="text", body=body)


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


def _appointment_id(action: str | None) -> uuid.UUID | None:
    if action is None or not action.startswith("appointment:"):
        return None
    try:
        return uuid.UUID(action.removeprefix("appointment:"))
    except ValueError:
        return None


def _context_uuid(context: dict[str, Any], key: str) -> uuid.UUID | None:
    value = _context_string(context, key)
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _context_from_existing_booking(booking) -> dict[str, Any]:
    requirements = booking.requirements
    context: dict[str, Any] = {
        "appointment_id": str(booking.appointment_id),
        "service_id": str(booking.service_id),
        "quantity": requirements.quantity or 1,
        "access_condition": requirements.access_condition.value,
    }
    if requirements.address is not None:
        context["service_address"] = requirements.address.to_snapshot()
    if requirements.tubing_meters is not None:
        context["tubing_meters"] = str(requirements.tubing_meters)
        context["tubing_length_answered"] = True
    if requirements.site_allowed_end is not None:
        context["site_allowed_end"] = requirements.site_allowed_end.strftime("%H:%M")
        context["site_limit_answered"] = True
    return context


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
            "tubing_meters",
            "tubing_length_answered",
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
    tubing_value = _context_string(context, "tubing_meters")
    tubing: Decimal | None = None
    if tubing_value is not None:
        try:
            tubing = Decimal(tubing_value)
        except Exception:
            tubing = None
    return BookingRequirements(
        quantity=quantity,
        access_condition=access,
        address=address,
        site_allowed_end=site_limit,
        tubing_meters=tubing,
    )


def _decimal_from_text(body: str | None) -> Decimal | None:
    normalized = normalize_portuguese(body or "").replace(",", ".")
    match = __import__("re").search(r"\b(\d+(?:\.\d{1,2})?)\b", normalized)
    if match is None:
        return None
    try:
        return Decimal(match.group(1))
    except Exception:
        return None


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


def _handoff_transition(
    body: str = "Seu atendimento foi encaminhado para uma pessoa da equipe.",
) -> ConversationTransition:
    return _transition(
        ConversationState.HUMAN_HANDOFF,
        {},
        _text_message(body),
        automation_enabled=False,
        handoff_status="waiting",
    )

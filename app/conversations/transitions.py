from __future__ import annotations

import uuid
import hashlib
import re
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
    ADDRESS_CITY_CONFIRM,
    ADDRESS_CITY_OTHER,
    EQUIPMENT_BOTH,
    EQUIPMENT_INSTALLATION,
    EQUIPMENT_PURCHASE,
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
    TUBING_CONFIRM,
    TUBING_UNKNOWN,
    ConversationState,
)
from app.conversations.outbound import (
    OutboundMessage,
    access_selection_message,
    address_city_confirmation_message,
    address_request_message,
    booking_cancelled_message,
    booking_completed_message,
    booking_confirmation_message,
    booking_unavailable_message,
    cancel_completed_message,
    cancel_confirmation_message,
    cancel_message,
    date_selection_message,
    equipment_purchase_clarification_message,
    existing_booking_selection_message,
    handoff_message,
    main_menu_message,
    name_request_message,
    no_services_message,
    quantity_selection_message,
    reschedule_completed_message,
    reschedule_confirmation_message,
    reschedule_message,
    service_selection_message,
    site_limit_message,
    slot_unavailable_message,
    tubing_confirmation_message,
    tubing_guidance_message,
    tubing_length_message,
    time_selection_message,
    weekday_selection_message,
)
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
    Interpretation,
    extract_customer_name,
    normalize_portuguese,
)
from app.conversations.dialogue import (
    customer_lead,
    date_short_label,
    dates_for_weekday,
    daypart_greeting,
    conversational_greeting,
    weekday_from_text,
    weekday_options,
    weekday_plural,
)
from app.conversations.service_semantics import semantic_service_score
from app.conversations.ports import (
    BookingAvailabilityPort,
    BookingConfirmation,
    BookingNotFound,
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
    if not conversation.assistant_enabled or not conversation.automation_enabled:
        return None

    state = _canonical_state(conversation.state)
    context = _clean_context(conversation.context)
    action = inbound.interactive_id
    interpreter = DeterministicConversationInterpreter()
    interpretation = interpreter.interpret(inbound.body)

    if interpretation.intent is ConversationIntent.HUMAN_HANDOFF:
        return _handoff_transition(conversation.handoff_message)

    stale_action = _stale_interactive_transition(
        state,
        action,
        context,
        customer_name=conversation.customer_name,
    )
    if stale_action is not None:
        return stale_action

    captured_name: str | None = interpretation.customer_name
    if captured_name is not None:
        conversation = replace(conversation, customer_name=captured_name)

    if state is ConversationState.CUSTOMER_NAME:
        transition = await _handle_customer_name(
            conversation,
            inbound,
            context,
            interpretation,
            booking_port,
        )
        if captured_name is not None:
            transition = replace(transition, customer_name=captured_name)
        return transition

    if conversation.customer_name is None:
        if captured_name is None:
            pending: dict[str, Any] = {}
            if _should_preserve_pending_message(inbound.body, interpretation):
                pending["pending_customer_message"] = inbound.body
            if action is not None:
                pending["pending_interactive_id"] = action
            return _name_request_transition(
                conversation,
                pending,
                interpretation,
                inbound.body,
            )

    transition = await _route_named_conversation(
        conversation,
        inbound,
        context,
        interpretation,
        booking_port,
    )
    if transition is not None and captured_name is not None:
        transition = replace(transition, customer_name=captured_name)
    return transition


async def _route_named_conversation(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition | None:
    state = _canonical_state(conversation.state)
    action = inbound.interactive_id
    customer_name = conversation.customer_name

    if interpretation.intent is ConversationIntent.RESCHEDULE and state is not ConversationState.RESCHEDULE:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="reschedule")
    if interpretation.intent is ConversationIntent.CANCEL and state is not ConversationState.CANCEL:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="cancel")

    question_answer = await _service_question_answer(
        inbound,
        context,
        interpretation,
        booking_port,
    )
    if (
        question_answer is not None
        and state in {
            ConversationState.START,
            ConversationState.MENU,
            ConversationState.COMPLETED,
            ConversationState.HUMAN_HANDOFF,
        }
        and not interpretation.has(ConversationIntent.BOOK)
        and not interpretation.has(ConversationIntent.AVAILABILITY)
    ):
        follow_up = (
            f"{question_answer}\n\n"
            f"Se quiser, {customer_lead(customer_name)}posso continuar com o agendamento."
        )
        return _transition(
            ConversationState.MENU,
            {},
            _text_message(follow_up),
        )

    booking_states = {
        ConversationState.BOOKING_QUANTITY,
        ConversationState.BOOKING_ACCESS,
        ConversationState.BOOKING_ADDRESS,
        ConversationState.BOOKING_TUBING,
        ConversationState.BOOKING_SITE_LIMIT,
        ConversationState.BOOKING_WEEKDAY,
        ConversationState.BOOKING_DATE,
        ConversationState.BOOKING_TIME,
        ConversationState.BOOKING_CONFIRM,
    }
    if (
        state in booking_states
        and interpretation.intent in {
            ConversationIntent.SERVICE_INTENT,
            ConversationIntent.EQUIPMENT_PURCHASE,
        }
        and not _has_service_question(interpretation)
    ):
        if interpretation.intent is ConversationIntent.EQUIPMENT_PURCHASE:
            return await _handle_service(
                inbound,
                {},
                None,
                booking_port,
                interpretation=interpretation,
                fallback_message=conversation.fallback_message,
                customer_name=customer_name,
            )
        try:
            port = _require_booking_port(booking_port)
            services = _snapshot_options(await port.list_services(inbound.business_id))
            matched = _service_for_interpretation(services, interpretation)
        except BookingPortUnavailable:
            matched = None
            services = ()
        current_id = _context_service_id(context)
        if (
            matched is not None
            and (current_id is None or matched.id != str(current_id))
        ):
            return await _handle_service(
                inbound,
                {},
                f"service:{matched.id}",
                booking_port,
                interpretation=interpretation,
                fallback_message=conversation.fallback_message,
                customer_name=customer_name,
            )

    greeting_prefix: str | None = None
    if (
        interpretation.has(ConversationIntent.GREETING)
        and state not in {
            ConversationState.START,
            ConversationState.MENU,
            ConversationState.COMPLETED,
            ConversationState.HUMAN_HANDOFF,
        }
    ):
        greeting_prefix = conversational_greeting(
            inbound.body,
            conversation.business_timezone,
            customer_name=conversation.customer_name,
            include_help=False,
        )

    if (
        state is ConversationState.BOOKING_ADDRESS
        and question_answer is not None
        and not _looks_like_address(inbound.body)
    ):
        transition = _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            address_request_message(),
        )
    elif state in {
        ConversationState.START,
        ConversationState.COMPLETED,
        ConversationState.HUMAN_HANDOFF,
    }:
        transition = await _handle_natural_start(
            conversation,
            inbound,
            interpretation,
            booking_port,
        )
    elif state is ConversationState.MENU:
        transition = await _handle_menu(
            inbound,
            action,
            booking_port,
            interpretation=interpretation,
            greeting_message=conversation.greeting_message,
            fallback_message=conversation.fallback_message,
            handoff_message=conversation.handoff_message,
            customer_name=customer_name,
            business_timezone=conversation.business_timezone,
        )
    elif state is ConversationState.BOOKING_SERVICE:
        transition = await _handle_service(
            inbound,
            context,
            action,
            booking_port,
            interpretation=interpretation,
            fallback_message=conversation.fallback_message,
            customer_name=customer_name,
        )
    elif state is ConversationState.BOOKING_QUANTITY:
        transition = await _handle_quantity(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_ACCESS:
        transition = await _handle_access(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_ADDRESS:
        transition = await _handle_address(
            inbound, context, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_TUBING:
        transition = await _handle_tubing(
            inbound, context, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_SITE_LIMIT:
        transition = await _handle_site_limit(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_WEEKDAY:
        transition = await _handle_weekday(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_DATE:
        transition = await _handle_date(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_TIME:
        transition = await _handle_time(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_CONFIRM:
        transition = await _handle_confirmation(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.RESCHEDULE:
        transition = await _handle_reschedule(inbound, context, action, booking_port)
    elif state is ConversationState.CANCEL:
        transition = await _handle_cancel(inbound, context, action, booking_port)
    else:
        transition = _transition(ConversationState.MENU, {}, main_menu_message())

    if question_answer is not None:
        transition = _prepend_transition_body(transition, question_answer)
    if greeting_prefix is not None:
        transition = _prepend_transition_body(transition, greeting_prefix)
    return transition


async def _handle_customer_name(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    if conversation.customer_name is not None:
        resumed = replace(
            conversation,
            state=ConversationState.START.value,
            context={},
        )
        return await _handle_natural_start(
            resumed,
            inbound,
            interpretation,
            booking_port,
        )

    customer_name = (
        interpretation.customer_name
        or extract_customer_name(inbound.body, allow_bare=True)
    )
    if customer_name is None:
        updated = dict(context)
        if (
            inbound.body
            and interpretation.intent
            not in {ConversationIntent.UNKNOWN, ConversationIntent.GREETING}
        ):
            previous = _context_string(updated, "pending_customer_message")
            updated["pending_customer_message"] = (
                f"{previous}\n{inbound.body}" if previous else inbound.body
            )
        if inbound.interactive_id is not None:
            updated["pending_interactive_id"] = inbound.interactive_id
        return _retry_or_handoff(
            ConversationState.CUSTOMER_NAME,
            updated,
            "customer_name",
            name_request_message(
                "Não consegui identificar seu nome. Pode me dizer só como gostaria de ser chamado?"
            ),
            handoff_body=(
                "Não consegui confirmar seu nome após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    pending_body = _context_string(context, "pending_customer_message")
    pending_action = _context_string(context, "pending_interactive_id")
    if pending_body is not None or pending_action is not None:
        replay = replace(
            inbound,
            body=pending_body,
            interactive_id=pending_action,
            message_type="interactive" if pending_action else "text",
        )
        replay_interpretation = _without_greeting(
            DeterministicConversationInterpreter().interpret(pending_body)
        )
        resumed = replace(
            conversation,
            state=(
                ConversationState.MENU.value
                if pending_action is not None and pending_action.startswith("menu.")
                else ConversationState.START.value
            ),
            context={},
            customer_name=customer_name,
        )
        transition = await _route_named_conversation(
            resumed,
            replay,
            {},
            replay_interpretation,
            booking_port,
        )
        if transition is None:
            transition = _transition(
                ConversationState.MENU,
                {},
                _text_message(f"Prazer, {customer_name}. Como posso te ajudar?"),
            )
        else:
            transition = _prepend_transition_body(
                transition,
                f"Prazer, {customer_name}.",
            )
    else:
        transition = _transition(
            ConversationState.MENU,
            {},
            _text_message(f"Prazer, {customer_name}. Como posso te ajudar?"),
        )
    return replace(transition, customer_name=customer_name)


def _name_request_transition(
    conversation: ConversationSnapshot,
    context: dict[str, Any],
    interpretation: Interpretation,
    inbound_body: str | None,
) -> ConversationTransition:
    if interpretation.has(ConversationIntent.GREETING):
        greeting = conversational_greeting(
            inbound_body,
            conversation.business_timezone,
            include_help=False,
        )
        body = f"{greeting} Antes de continuarmos, qual é o seu nome?"
    else:
        greeting = daypart_greeting(conversation.business_timezone)
        body = (
            f"{greeting}! Claro. Antes de continuarmos, qual é o seu nome?"
        )
    return _transition(
        ConversationState.CUSTOMER_NAME,
        context,
        name_request_message(body),
    )


def _conversation_greeting(conversation: ConversationSnapshot) -> str:
    greeting = daypart_greeting(conversation.business_timezone)
    if conversation.customer_name:
        return f"{greeting}, {conversation.customer_name}!"
    return f"{greeting}!"


def _has_service_question(interpretation: Interpretation) -> bool:
    return any(
        interpretation.has(intent)
        for intent in (
            ConversationIntent.PRICE_QUESTION,
            ConversationIntent.DURATION_QUESTION,
            ConversationIntent.SERVICE_QUESTION,
        )
    )


async def _service_question_answer(
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> str | None:
    if not _has_service_question(interpretation):
        return None
    try:
        port = _require_booking_port(booking_port)
        services = _snapshot_options(await port.list_services(inbound.business_id))
    except BookingPortUnavailable:
        return None

    matched = _service_for_interpretation(services, interpretation)
    current_id = _context_service_id(context)
    target = matched
    if target is None and current_id is not None:
        target = next(
            (service for service in services if service.id == str(current_id)),
            None,
        )
    if target is None:
        return None

    try:
        target_id = uuid.UUID(target.id)
    except ValueError:
        return None
    requirements = (
        _requirements_from_context(context)
        if current_id == target_id
        else BookingRequirements()
    )
    parts: list[str] = []
    plan: BookingPlan | None = None
    if (
        interpretation.has(ConversationIntent.PRICE_QUESTION)
        or interpretation.has(ConversationIntent.DURATION_QUESTION)
    ):
        try:
            plan = await port.estimate(
                inbound.business_id,
                target_id,
                requirements,
            )
        except BookingRequiresHandoff:
            plan = None

    if interpretation.has(ConversationIntent.PRICE_QUESTION):
        if plan is None or plan.service.estimated_price is None:
            parts.append(
                f"O valor de {target.label} depende de uma avaliação da equipe."
            )
        else:
            qualifier = (
                "é"
                if plan.service.pricing_type is PricingType.FIXED
                else "está estimado em"
            )
            parts.append(
                f"O valor de {target.label} {qualifier} "
                f"{_format_brl(plan.service.estimated_price)}."
            )

    if interpretation.has(ConversationIntent.DURATION_QUESTION):
        if plan is None:
            parts.append(
                f"A duração de {target.label} depende de uma avaliação da equipe."
            )
        else:
            parts.append(
                f"A duração estimada de {target.label} é de "
                f"{_format_duration(plan.service.estimated_duration_minutes)}."
            )

    if interpretation.has(ConversationIntent.SERVICE_QUESTION):
        details_loader = getattr(port, "get_service_details", None)
        details = None
        if callable(details_loader):
            try:
                details = await details_loader(inbound.business_id, target_id)
            except BookingRequiresHandoff:
                details = None
        description = getattr(details, "description", None)
        if isinstance(description, str) and description.strip():
            parts.append(description.strip())
        else:
            parts.append(
                "Esse detalhe específico não está descrito no catálogo do serviço. "
                "Para não te passar uma informação incorreta, prefiro confirmar "
                "somente o que está cadastrado."
            )

    return " ".join(parts) if parts else None


def _prepend_transition_body(
    transition: ConversationTransition,
    prefix: str,
) -> ConversationTransition:
    body = transition.outbound.body
    combined = prefix if not body else f"{prefix}\n\n{body}"
    return replace(
        transition,
        outbound=replace(transition.outbound, body=combined[:1024]),
    )


def _stale_interactive_transition(
    state: ConversationState,
    action: str | None,
    context: dict[str, Any],
    *,
    customer_name: str | None,
) -> ConversationTransition | None:
    if action is None:
        return None

    if state is ConversationState.CUSTOMER_NAME:
        return _transition(
            state,
            context,
            name_request_message(
                "Essa opção era de uma etapa anterior. Sem problema. "
                "Para continuarmos, qual é o seu nome?"
            ),
        )
    if state is ConversationState.BOOKING_ADDRESS:
        expected_city_action = (
            _context_string(context, "pending_service_address") is not None
            and action in {ADDRESS_CITY_CONFIRM, ADDRESS_CITY_OTHER}
        )
        if not expected_city_action:
            return _transition(
                state,
                context,
                address_request_message(
                    "Essa opção era de uma etapa anterior. "
                    "Pode me enviar o endereço do atendimento, com rua, número e cidade?"
                ),
            )
    if (
        state is ConversationState.BOOKING_TUBING
        and action not in {TUBING_CONFIRM, TUBING_UNKNOWN}
    ):
        return _transition(
            state,
            context,
            tubing_length_message(),
        )
    return None


async def _tubing_prompt_transition(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    service_id: uuid.UUID,
    *,
    body: str | None = None,
) -> ConversationTransition:
    included_meters: Decimal | None = None
    extra_meter_price: Decimal | None = None
    try:
        details = await port.get_service_details(
            inbound.business_id,
            service_id,
        )
        included_meters = details.included_tubing_meters
        extra_meter_price = details.extra_tubing_price
    except BookingRequiresHandoff:
        pass

    question = (
        _text_message(body)
        if body is not None
        else tubing_length_message()
    )
    guidance = tubing_guidance_message(
        included_meters,
        extra_meter_price,
    )
    return _transition(
        ConversationState.BOOKING_TUBING,
        context,
        question,
        follow_ups=(guidance,),
    )


def _tubing_unknown_text(normalized: str) -> bool:
    phrases = (
        "nao sei",
        "nao tenho certeza",
        "nao faco ideia",
        "sem ideia",
        "dificil dizer",
        "nao consigo estimar",
    )
    return any(phrase in normalized for phrase in phrases)


def _tubing_needs_confirmation(normalized: str) -> bool:
    uncertainty = (
        "acho",
        "acredito",
        "talvez",
        "creio",
        "imagino",
        "nao tenho certeza",
        "dificil",
    )
    return any(
        f" {phrase} " in f" {normalized} "
        for phrase in uncertainty
    )


def _has_substantive_intent(interpretation: Interpretation) -> bool:
    return any(
        interpretation.has(intent)
        for intent in (
            ConversationIntent.BOOK,
            ConversationIntent.RESCHEDULE,
            ConversationIntent.CANCEL,
            ConversationIntent.HUMAN_HANDOFF,
            ConversationIntent.SERVICE_INTENT,
            ConversationIntent.EQUIPMENT_PURCHASE,
            ConversationIntent.AVAILABILITY,
            ConversationIntent.PRICE_QUESTION,
            ConversationIntent.DURATION_QUESTION,
            ConversationIntent.SERVICE_QUESTION,
        )
    )


def _should_preserve_pending_message(
    body: str | None,
    interpretation: Interpretation,
) -> bool:
    if not body or not body.strip():
        return False
    if _has_substantive_intent(interpretation):
        return True
    normalized = interpretation.normalized_text
    social_only = {
        "oi",
        "ola",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "como vai",
        "bom dia tudo bem",
        "boa tarde tudo bem",
        "boa noite tudo bem",
        "oi tudo bem",
        "ola tudo bem",
    }
    return normalized not in social_only


def _without_greeting(interpretation: Interpretation) -> Interpretation:
    intents = frozenset(
        intent
        for intent in interpretation.intents
        if intent is not ConversationIntent.GREETING
    )
    primary = interpretation.intent
    if primary is ConversationIntent.GREETING:
        precedence = (
            ConversationIntent.HUMAN_HANDOFF,
            ConversationIntent.RESCHEDULE,
            ConversationIntent.CANCEL,
            ConversationIntent.EQUIPMENT_PURCHASE,
            ConversationIntent.SERVICE_INTENT,
            ConversationIntent.AVAILABILITY,
            ConversationIntent.BOOK,
            ConversationIntent.PRICE_QUESTION,
            ConversationIntent.DURATION_QUESTION,
            ConversationIntent.SERVICE_QUESTION,
        )
        primary = next(
            (intent for intent in precedence if intent in intents),
            ConversationIntent.UNKNOWN,
        )
    if not intents:
        intents = frozenset({primary})
    return replace(
        interpretation,
        intent=primary,
        intents=intents,
    )


def _repair_attempts(context: dict[str, Any]) -> dict[str, int]:
    raw = context.get("repair_attempts")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }


def _retry_or_handoff(
    state: ConversationState,
    context: dict[str, Any],
    slot: str,
    outbound: OutboundMessage,
    *,
    handoff_body: str,
) -> ConversationTransition:
    attempts = _repair_attempts(context)
    next_attempt = attempts.get(slot, 0) + 1
    if next_attempt >= 2:
        return _handoff_transition(handoff_body)
    attempts[slot] = next_attempt
    return _transition(
        state,
        {**context, "repair_attempts": attempts},
        outbound,
    )


def _clear_repair_attempt(
    context: dict[str, Any],
    slot: str,
) -> dict[str, Any]:
    updated = dict(context)
    attempts = _repair_attempts(updated)
    attempts.pop(slot, None)
    if attempts:
        updated["repair_attempts"] = attempts
    else:
        updated.pop("repair_attempts", None)
    return updated


_BRAZIL_STATE_NAMES = {
    "acre", "alagoas", "amapa", "amazonas", "bahia", "ceara",
    "distrito federal", "espirito santo", "goias", "maranhao",
    "mato grosso", "mato grosso do sul", "minas gerais", "para",
    "paraiba", "parana", "pernambuco", "piaui", "rio de janeiro",
    "rio grande do norte", "rio grande do sul", "rondonia", "roraima",
    "santa catarina", "sao paulo", "sergipe", "tocantins",
}
_BRAZIL_STATE_CODES = {
    "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma",
    "mt", "ms", "mg", "pa", "pb", "pr", "pe", "pi", "rj", "rn",
    "rs", "ro", "rr", "sc", "sp", "se", "to",
}
_NEIGHBORHOOD_MARKERS = {
    "bairro", "jardim", "jd", "parque", "pq", "vila", "vl",
    "residencial", "loteamento", "conjunto", "centro",
}


def _expand_address_abbreviations(value: str) -> str:
    replacements = {
        "pq": "Parque",
        "jd": "Jardim",
        "av": "Avenida",
        "r": "Rua",
        "vl": "Vila",
        "rod": "Rodovia",
        "trav": "Travessa",
        "al": "Alameda",
    }
    expanded = value.strip()
    for abbreviation, replacement in replacements.items():
        expanded = re.sub(
            rf"\b{re.escape(abbreviation)}\.?\b",
            replacement,
            expanded,
            flags=re.IGNORECASE,
        )
    return " ".join(expanded.split())


def _address_has_city_or_state(
    value: str,
    *,
    business_city: str | None,
    business_state: str | None,
) -> bool:
    normalized = normalize_portuguese(value)
    padded = f" {normalized} "

    if isinstance(business_city, str) and business_city.strip():
        city = normalize_portuguese(business_city)
        if f" {city} " in padded:
            return True

    tokens = normalized.split()
    if any(code in tokens for code in _BRAZIL_STATE_CODES):
        return True
    if any(f" {state} " in padded for state in _BRAZIL_STATE_NAMES):
        return True

    # Rua..., número..., cidade: último segmento textual sem marcador de bairro.
    segments = [
        normalize_portuguese(segment)
        for segment in re.split(r"[,\n]+", value)
        if normalize_portuguese(segment)
    ]
    if len(segments) >= 3:
        last = segments[-1]
        first_word = last.split()[0] if last.split() else ""
        if (
            not any(character.isdigit() for character in last)
            and first_word not in _NEIGHBORHOOD_MARKERS
            and 1 <= len(last.split()) <= 5
        ):
            return True

    # Forma comum sem vírgulas: "Rua 28, Parque Imperial Jacareí" / "pq imperial jacarei".
    for marker in _NEIGHBORHOOD_MARKERS - {"centro"}:
        match = re.search(
            rf"\b{marker}\b\s+([a-z0-9]+(?:\s+[a-z0-9]+){{1,5}})$",
            normalized,
        )
        if match and len(match.group(1).split()) >= 2:
            return True
    return False


def _city_from_label(value: str) -> str:
    return re.split(r"\s+-\s+", value.strip(), maxsplit=1)[0].strip()


def _state_from_label(value: str) -> str | None:
    parts = re.split(r"\s+-\s+", value.strip(), maxsplit=1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


def _address_success_context(
    context: dict[str, Any],
    service_id: uuid.UUID,
    address: ServiceAddress,
) -> dict[str, Any]:
    updated = _clear_repair_attempt(context, "address")
    updated = _clear_repair_attempt(updated, "city")
    updated.update(
        {
            "service_id": str(service_id),
            "service_address": address.to_snapshot(),
        }
    )
    updated.pop("pending_service_address", None)
    updated.pop("pending_address_city_guess", None)
    updated.pop("awaiting_address_city", None)
    return updated


def _friendly_fallback(configured: str) -> str:
    normalized = normalize_portuguese(configured)
    if normalized.startswith("nao entendi"):
        return (
            "Posso ajudar com limpeza, instalação, manutenção, recarga de gás, "
            "agendamento, reagendamento ou cancelamento. "
            "Me conte em poucas palavras o que você precisa."
        )
    return configured


def _looks_like_city(value: str | None) -> bool:
    normalized = normalize_portuguese(value or "").strip()
    if len(normalized) < 3 or len(normalized) > 120:
        return False
    if any(character.isdigit() for character in normalized):
        return False
    if normalized in {
        "nao sei",
        "nao faco ideia",
        "nao tenho certeza",
        "sei la",
        "talvez",
        "nao lembro",
    }:
        return False
    words = [word for word in normalized.split() if word]
    return 1 <= len(words) <= 8


def _address_needs_city(
    value: str,
    *,
    business_city: str | None,
    business_state: str | None,
) -> bool:
    normalized = normalize_portuguese(value)
    compact = re.sub(r"\s+", " ", normalized).strip()
    if re.search(r"\b\d{5}\s*-?\s*\d{3}\b", compact):
        return False

    if isinstance(business_city, str) and business_city.strip():
        normalized_city = normalize_portuguese(business_city)
        if normalized_city in compact:
            return False

    if isinstance(business_state, str) and business_state.strip():
        state = normalize_portuguese(business_state).strip()
        if re.search(rf"(?:^|[\s,\-/]){re.escape(state)}(?:$|[\s,\-/])", compact):
            return False

    # Sem CEP, UF ou a cidade conhecida da empresa, o endereço ainda pode
    # estar ambíguo. Confirmar a cidade é mais seguro do que deixar a API
    # de rotas geocodificar uma rua homônima em outro município.
    return True


def _looks_like_address(value: str | None) -> bool:
    normalized = normalize_portuguese(value or "")
    if not normalized:
        return False
    address_words = (
        "rua",
        "avenida",
        "av",
        "travessa",
        "alameda",
        "estrada",
        "rodovia",
        "praca",
        "bairro",
        "numero",
    )
    has_number = bool(re.search(r"\b\d{1,6}\b", normalized))
    has_address_word = any(
        f" {word} " in f" {normalized} "
        for word in address_words
    )
    has_postal_code = bool(re.search(r"\b\d{5}\s?\d{3}\b", normalized))
    return has_postal_code or (has_number and has_address_word)


def _format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutos"
    hours, remainder = divmod(minutes, 60)
    if remainder == 0:
        return f"{hours} hora" if hours == 1 else f"{hours} horas"
    hour_label = "hora" if hours == 1 else "horas"
    return f"{hours} {hour_label} e {remainder} minutos"


async def _handle_natural_start(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    if (
        interpretation.intent is ConversationIntent.GREETING
        and not _has_substantive_intent(interpretation)
    ):
        return _transition(
            ConversationState.MENU,
            {},
            _text_message(
                conversational_greeting(
                    inbound.body,
                    conversation.business_timezone,
                    customer_name=conversation.customer_name,
                    include_help=True,
                )
            ),
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
        ConversationIntent.EQUIPMENT_PURCHASE,
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

        transition = await _handle_service(
            inbound,
            {},
            None,
            port,
            interpretation=interpretation,
            fallback_message=conversation.fallback_message,
            customer_name=conversation.customer_name,
        )
        if interpretation.has(ConversationIntent.GREETING):
            transition = _prepend_transition_body(
                transition,
                conversational_greeting(
                    inbound.body,
                    conversation.business_timezone,
                    customer_name=conversation.customer_name,
                    include_help=False,
                ),
            )
        return transition
    return _transition(
        ConversationState.MENU,
        {},
        _text_message(_friendly_fallback(conversation.fallback_message)),
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
    customer_name: str | None = None,
    business_timezone: str = "America/Sao_Paulo",
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
            customer_name=customer_name,
            business_timezone=business_timezone,
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
    customer_name: str | None = None,
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

    normalized = normalize_portuguese(inbound.body or "")
    clarification = _context_string(context, "service_clarification")
    if action in {EQUIPMENT_PURCHASE, EQUIPMENT_BOTH}:
        return _handoff_transition(
            "Entendi. Para compra do aparelho, a equipe precisa confirmar modelos, "
            "disponibilidade e valores. Vou encaminhar seu atendimento para continuarem com você."
        )
    if action == EQUIPMENT_INSTALLATION:
        interpretation = DeterministicConversationInterpreter().interpret(
            "instalação de ar condicionado split"
        )
        action = None
    elif clarification == "equipment_purchase":
        if normalized in {
            "comprar",
            "comprar aparelho",
            "so comprar",
            "somente comprar",
            "compra",
            "os dois",
            "ambos",
            "compra e instalacao",
            "comprar e instalar",
        }:
            return _handoff_transition(
                "Entendi. Como envolve a compra do aparelho, a equipe precisa confirmar "
                "modelos, disponibilidade e valores. Vou encaminhar seu atendimento "
                "para continuarem com você."
            )
        if "instal" in normalized:
            interpretation = DeterministicConversationInterpreter().interpret(
                "instalação de ar condicionado split"
            )
        elif (
            interpretation is not None
            and interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
        ):
            return _handoff_transition(
                "Entendi. Para compra do aparelho, a equipe precisa confirmar modelos, "
                "disponibilidade e valores. Vou encaminhar seu atendimento para continuarem com você."
            )
        else:
            return _retry_or_handoff(
                ConversationState.BOOKING_SERVICE,
                context,
                "service_clarification",
                equipment_purchase_clarification_message(retry=True),
                handoff_body=(
                    "Não consegui confirmar com segurança se você quer comprar o aparelho, "
                    "instalação ou os dois. Vou chamar uma pessoa da equipe para continuar."
                ),
            )
    elif (
        interpretation is not None
        and interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
    ):
        updated = {
            **context,
            "service_clarification": "equipment_purchase",
        }
        return _transition(
            ConversationState.BOOKING_SERVICE,
            updated,
            equipment_purchase_clarification_message(),
        )

    service_id = _service_id(action)
    if service_id is None:
        interpretation = interpretation or DeterministicConversationInterpreter().interpret(
            inbound.body
        )
        matched = _service_for_interpretation(services, interpretation)
        service_id = uuid.UUID(matched.id) if matched is not None else None
    if (
        action is not None
        and action.startswith("service:")
        and (service_id is None or not _option_exists(services, str(service_id)))
    ):
        return _transition(
            ConversationState.BOOKING_SERVICE,
            context,
            service_selection_message(
                services,
                body="Essa opção não está mais disponível. Escolha um serviço para continuar.",
            ),
        )
    if service_id is None or not _option_exists(services, str(service_id)):
        if interpretation and interpretation.intent in {
            ConversationIntent.BOOK,
            ConversationIntent.AVAILABILITY,
        }:
            message = service_selection_message(
                services,
                body="Claro. Qual serviço você quer agendar?",
            )
        elif inbound.body and inbound.body.strip():
            message = service_selection_message(
                services,
                body=(
                    "Não consegui identificar com segurança o serviço. "
                    "Você pode escolher uma das opções abaixo?"
                ),
            )
        else:
            message = service_selection_message(services)
        return _retry_or_handoff(
            ConversationState.BOOKING_SERVICE,
            context,
            "service",
            message,
            handoff_body=(
                "Não consegui identificar o serviço com segurança após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    try:
        intake = await port.get_service_intake(inbound.business_id, service_id)
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if (
        not intake.automatic_booking
        or intake.pricing_type is PricingType.HUMAN_QUOTE
    ):
        return _handoff_for_reason("human_quote")
    return await _advance_intake(
        inbound,
        port,
        intake,
        {"service_id": str(service_id)},
        services=services,
        customer_name=customer_name,
    )


async def _handle_quantity(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    quantity = _quantity(action, inbound.body)
    if quantity is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_QUANTITY,
            context,
            "quantity",
            quantity_selection_message(),
            handoff_body=(
                "Não consegui confirmar a quantidade após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    return await _advance_intake(
        inbound,
        port,
        intake,
        {
            **_clear_repair_attempt(context, "quantity"),
            "service_id": str(service_id),
            "quantity": quantity,
        },
        customer_name=customer_name,
    )


async def _handle_access(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    access = {
        ACCESS_NORMAL: AccessCondition.NORMAL,
        ACCESS_DIFFICULT: AccessCondition.DIFFICULT,
        ACCESS_UNKNOWN: AccessCondition.UNKNOWN,
    }.get(action)
    if access is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_ACCESS,
            context,
            "access",
            access_selection_message(),
            handoff_body=(
                "Não consegui confirmar a condição de acesso após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = _clear_repair_attempt(context, "access")
    return await _advance_intake(
        inbound,
        port,
        intake,
        {
            **context,
            "service_id": str(service_id),
            "access_condition": access.value,
        },
        customer_name=customer_name,
    )


async def _handle_address(
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    try:
        details = await port.get_service_details(
            inbound.business_id,
            service_id,
        )
    except BookingRequiresHandoff:
        details = None
    business_city = getattr(details, "business_city", None)
    business_state = getattr(details, "business_state", None)

    raw_value = (inbound.body or "").strip()
    value = _expand_address_abbreviations(raw_value)
    normalized = normalize_portuguese(value)
    pending_address = _context_string(context, "pending_service_address")
    city_guess = _context_string(context, "pending_address_city_guess")
    awaiting_city = context.get("awaiting_address_city") is True

    if pending_address is not None:
        pending_address = _expand_address_abbreviations(pending_address)
        if (
            inbound.interactive_id == ADDRESS_CITY_CONFIRM
            or normalized in {"sim", "isso", "isso mesmo", "correto"}
        ):
            if city_guess is None:
                return _retry_or_handoff(
                    ConversationState.BOOKING_ADDRESS,
                    {**context, "awaiting_address_city": True},
                    "city",
                    address_request_message(
                        "Para eu localizar corretamente, qual é a cidade desse endereço?"
                    ),
                    handoff_body=(
                        "Não consegui confirmar a cidade com segurança após duas tentativas. "
                        "Vou chamar uma pessoa da equipe para continuar com você."
                    ),
                )
            address = ServiceAddress(
                address_line=pending_address,
                city=_city_from_label(city_guess),
                state=_state_from_label(city_guess),
            )
            return await _advance_intake(
                inbound,
                port,
                intake,
                _address_success_context(context, service_id, address),
                customer_name=customer_name,
            )

        if (
            inbound.interactive_id == ADDRESS_CITY_OTHER
            or normalized in {"nao", "não", "outra cidade"}
        ):
            updated = {
                **context,
                "awaiting_address_city": True,
            }
            updated.pop("pending_address_city_guess", None)
            return _retry_or_handoff(
                ConversationState.BOOKING_ADDRESS,
                updated,
                "city",
                address_request_message(
                    "Certo. Me diga apenas a cidade desse endereço."
                ),
                handoff_body=(
                    "Não consegui confirmar a cidade com segurança após duas tentativas. "
                    "Vou chamar uma pessoa da equipe para continuar com você."
                ),
            )

        if awaiting_city:
            if _looks_like_city(raw_value):
                address = ServiceAddress(
                    address_line=pending_address,
                    city=raw_value,
                )
                return await _advance_intake(
                    inbound,
                    port,
                    intake,
                    _address_success_context(context, service_id, address),
                    customer_name=customer_name,
                )
            return _retry_or_handoff(
                ConversationState.BOOKING_ADDRESS,
                context,
                "city",
                address_request_message(
                    "Não consegui reconhecer a cidade. Pode me informar somente o nome da cidade?"
                ),
                handoff_body=(
                    "Não consegui confirmar a cidade com segurança após duas tentativas. "
                    "Vou chamar uma pessoa da equipe para continuar com você."
                ),
            )

        # O cliente pode ignorar os botões e corrigir o endereço completo por texto.
        if _looks_like_address(value):
            if _address_has_city_or_state(
                value,
                business_city=business_city,
                business_state=business_state,
            ):
                address = ServiceAddress(address_line=value)
                return await _advance_intake(
                    inbound,
                    port,
                    intake,
                    _address_success_context(context, service_id, address),
                    customer_name=customer_name,
                )
            updated = {
                **context,
                "pending_service_address": value,
            }
            return _retry_or_handoff(
                ConversationState.BOOKING_ADDRESS,
                updated,
                "city",
                address_request_message(
                    "Entendi o endereço. Só falta a cidade. Qual é?"
                ),
                handoff_body=(
                    "Não consegui confirmar a cidade com segurança após duas tentativas. "
                    "Vou chamar uma pessoa da equipe para continuar com você."
                ),
            )

    if normalized in {"nao sei", "nao tenho certeza"}:
        return _retry_or_handoff(
            ConversationState.BOOKING_ADDRESS,
            context,
            "address",
            address_request_message(
                "Sem problema. Para consultar a agenda, me envie rua, número e cidade."
            ),
            handoff_body=(
                "Não consegui obter um endereço suficiente após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    if (
        len(value) < 5
        or len(value) > 500
        or not _looks_like_address(value)
    ):
        return _retry_or_handoff(
            ConversationState.BOOKING_ADDRESS,
            context,
            "address",
            address_request_message(
                "Não consegui identificar o endereço. Tente me enviar rua, número, bairro e cidade."
            ),
            handoff_body=(
                "Não consegui obter um endereço suficiente após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    if not _address_has_city_or_state(
        value,
        business_city=business_city,
        business_state=business_state,
    ):
        updated = {
            **context,
            "service_id": str(service_id),
            "pending_service_address": value,
        }
        if isinstance(business_city, str) and business_city.strip():
            city_label = business_city.strip()
            if isinstance(business_state, str) and business_state.strip():
                city_label = f"{city_label} - {business_state.strip()}"
            updated["pending_address_city_guess"] = city_label
            updated.pop("awaiting_address_city", None)
            return _transition(
                ConversationState.BOOKING_ADDRESS,
                updated,
                address_city_confirmation_message(city_label),
            )
        updated["awaiting_address_city"] = True
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            updated,
            address_request_message(
                "Só falta a cidade para eu localizar corretamente. Qual é?"
            ),
        )

    address = ServiceAddress(address_line=value)
    return await _advance_intake(
        inbound,
        port,
        intake,
        _address_success_context(context, service_id, address),
        customer_name=customer_name,
    )

async def _handle_tubing(
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    pending_meters = _context_string(context, "pending_tubing_meters")
    if inbound.interactive_id == TUBING_CONFIRM and pending_meters is not None:
        updated = {
            **context,
            "service_id": str(service_id),
            "tubing_length_answered": True,
            "tubing_meters": pending_meters,
        }
        updated.pop("pending_tubing_meters", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )
    if inbound.interactive_id == TUBING_UNKNOWN:
        updated = {
            **context,
            "service_id": str(service_id),
            "tubing_length_answered": True,
        }
        updated.pop("pending_tubing_meters", None)
        updated.pop("tubing_meters", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )

    normalized = normalize_portuguese(inbound.body or "")
    updated = {
        **context,
        "service_id": str(service_id),
        "tubing_length_answered": True,
    }
    if _tubing_unknown_text(normalized):
        updated.pop("pending_tubing_meters", None)
        updated.pop("tubing_meters", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )

    meters = _decimal_from_text(inbound.body)
    if meters is None or meters <= 0 or meters > Decimal("100"):
        retried = _retry_or_handoff(
            ConversationState.BOOKING_TUBING,
            context,
            "tubing",
            tubing_length_message(),
            handoff_body=(
                "Não consegui confirmar a metragem após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
        if retried.state is ConversationState.HUMAN_HANDOFF:
            return retried
        return await _tubing_prompt_transition(
            inbound,
            port,
            retried.context,
            service_id,
            body=(
                "Não consegui entender a metragem. Você consegue me passar uma estimativa?"
            ),
        )

    if _tubing_needs_confirmation(normalized):
        pending = {
            **context,
            "service_id": str(service_id),
            "pending_tubing_meters": str(meters),
        }
        pending.pop("tubing_length_answered", None)
        return _transition(
            ConversationState.BOOKING_TUBING,
            pending,
            tubing_confirmation_message(meters),
        )

    updated = _clear_repair_attempt(updated, "tubing")
    updated.pop("pending_tubing_meters", None)
    updated["tubing_meters"] = str(meters)
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_site_limit(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    site_limit = _site_limit(action, inbound.body)
    if site_limit is False:
        return _retry_or_handoff(
            ConversationState.BOOKING_SITE_LIMIT,
            context,
            "site_limit",
            site_limit_message(),
            handoff_body=(
                "Não consegui confirmar o horário limite após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    updated = {
        **_clear_repair_attempt(context, "site_limit"),
        "service_id": str(service_id),
        "site_limit_answered": True,
    }
    if isinstance(site_limit, time):
        updated["site_allowed_end"] = site_limit.strftime("%H:%M")
    else:
        updated.pop("site_allowed_end", None)
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_weekday(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_WEEKDAY,
            context,
            booking_unavailable_message(),
        )
    service_id = _context_service_id(context)
    if service_id is None:
        return await _restart_service_selection(inbound, port)
    requirements = _requirements_from_context(context)
    try:
        dates = _snapshot_options(
            await port.list_dates(inbound.business_id, service_id, requirements)
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if not dates:
        return await _restart_service_selection(
            inbound,
            port,
            body="Não encontrei datas disponíveis para esse serviço.",
        )

    exact_date = _date_from_text(inbound.body, dates)
    if exact_date is not None:
        return await _offer_times_for_date(
            inbound,
            port,
            context,
            service_id,
            exact_date,
            requirements,
            customer_name=customer_name,
        )

    weekday = _selected_weekday(action)
    if weekday is None:
        weekday = weekday_from_text(inbound.body)
    if weekday is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_WEEKDAY,
            context,
            "weekday",
            weekday_selection_message(
                weekday_options(dates),
                body=(
                    f"{customer_lead(customer_name)}Tenho disponibilidade em vários dias. "
                    "Qual dia da semana fica melhor para o seu atendimento?"
                ),
            ),
            handoff_body=(
                "Não consegui confirmar o dia da semana após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    matches = dates_for_weekday(dates, weekday)
    if not matches:
        return _transition(
            ConversationState.BOOKING_WEEKDAY,
            _intake_context(context),
            weekday_selection_message(
                weekday_options(dates),
                body=(
                    f"Não encontrei horário disponível em {weekday_plural(weekday)}. "
                    "Qual outro dia da semana fica melhor?"
                ),
            ),
        )
    if len(matches) == 1:
        return await _offer_times_for_date(
            inbound,
            port,
            context,
            service_id,
            matches[0].id,
            requirements,
            customer_name=customer_name,
        )
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(
            matches,
            body=(
                f"{customer_lead(customer_name)}Tenho estas {weekday_plural(weekday)} "
                "disponíveis. Qual data fica melhor para o seu atendimento?"
            ),
        ),
    )


async def _handle_date(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if not dates:
        return await _restart_service_selection(
            inbound,
            port,
            body="Não há datas disponíveis. Escolha outro serviço.",
        )

    selected_date = _selected_date(action) or _date_from_text(inbound.body, dates)
    if action is not None and action.startswith("date:") and _selected_date(action) is None:
        return _transition(
            ConversationState.BOOKING_DATE,
            context,
            date_selection_message(
                dates,
                body="Essa opção de data não é mais válida. Escolha uma data disponível.",
            ),
        )
    if selected_date is None:
        weekday = weekday_from_text(inbound.body)
        if weekday is not None and len(dates_for_weekday(dates, weekday)) > 1:
            return await _handle_weekday(
                inbound,
                context,
                f"weekday:{weekday}",
                port,
                customer_name=customer_name,
            )
        if len(dates) > 10:
            return _retry_or_handoff(
                ConversationState.BOOKING_WEEKDAY,
                context,
                "date",
                weekday_selection_message(
                    weekday_options(dates),
                    body=(
                        f"{customer_lead(customer_name)}Tenho disponibilidade em vários dias. "
                        "Qual dia da semana fica melhor para o seu atendimento?"
                    ),
                ),
                handoff_body=(
                    "Não consegui confirmar a data após duas tentativas. "
                    "Vou chamar uma pessoa da equipe para continuar com você."
                ),
            )
        return _retry_or_handoff(
            ConversationState.BOOKING_DATE,
            context,
            "date",
            date_selection_message(
                dates,
                body=(
                    f"{customer_lead(customer_name)}Qual dia fica melhor "
                    "para o seu atendimento?"
                ),
            ),
            handoff_body=(
                "Não consegui confirmar a data após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    if not _option_exists(dates, selected_date):
        return _retry_or_handoff(
            ConversationState.BOOKING_DATE,
            context,
            "date",
            date_selection_message(
                dates,
                body="Essa data não apareceu como disponível. Qual outra data fica melhor?"
            ),
            handoff_body=(
                "Não consegui confirmar a data após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = _clear_repair_attempt(context, "date")
    return await _offer_times_for_date(
        inbound,
        port,
        context,
        service_id,
        selected_date,
        requirements,
        customer_name=customer_name,
    )


async def _handle_time(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if not times:
        return await _return_to_dates(
            inbound,
            port,
            service_id,
            context,
            requirements,
            customer_name=customer_name,
        )

    selected_time = _selected_time(action) or _time_from_text(inbound.body, times)
    if (
        action is not None
        and action.startswith("time:")
        and (
            _selected_time(action) is None
            or not _option_exists(times, _selected_time(action) or "")
        )
    ):
        return _transition(
            ConversationState.BOOKING_TIME,
            context,
            time_selection_message(
                times,
                body=_time_prompt(selected_date, times, customer_name),
            ),
        )
    if selected_time is None or not _option_exists(times, selected_time):
        return _retry_or_handoff(
            ConversationState.BOOKING_TIME,
            context,
            "time",
            time_selection_message(
                times,
                body=_time_prompt(selected_date, times, customer_name),
            ),
            handoff_body=(
                "Não consegui confirmar o horário após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = _clear_repair_attempt(context, "time")
    return await _booking_confirm_transition(
        inbound,
        port,
        context,
        service_id,
        selected_date,
        selected_time,
        requirements,
        customer_name=customer_name,
    )


async def _handle_confirmation(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
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

    if action not in {BOOKING_CONFIRM, BOOKING_BACK}:
        return _retry_or_handoff(
            ConversationState.BOOKING_CONFIRM,
            context,
            "confirmation",
            booking_confirmation_message(
                await _confirmation_body(
                    inbound,
                    port,
                    context,
                    service_id,
                    selected_date,
                    selected_time,
                    requirements,
                    customer_name=customer_name,
                )
            ),
            handoff_body=(
                "Não consegui confirmar sua decisão após duas tentativas. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = _clear_repair_attempt(context, "confirmation")

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
            time_selection_message(
                times,
                body=_time_prompt(selected_date, times, customer_name),
            ),
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if not isinstance(confirmation, BookingConfirmation):
        return _transition(
            ConversationState.BOOKING_CONFIRM,
            context,
            booking_unavailable_message(),
        )
    return _transition(
        ConversationState.COMPLETED,
        {},
        booking_completed_message(
            f"Agendamento confirmado para {date_short_label(selected_date)} "
            f"às {selected_time}."
        ),
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
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
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
    customer_name: str | None = None,
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
        service_id = _context_service_id(context)
        if service_id is None:
            return await _restart_service_selection(inbound, port)
        return await _tubing_prompt_transition(
            inbound,
            port,
            context,
            service_id,
        )
    if intake.asks_site_time_limit and context.get("site_limit_answered") is not True:
        return _transition(
            ConversationState.BOOKING_SITE_LIMIT,
            context,
            site_limit_message(),
        )
    return await _offer_dates(
        inbound,
        port,
        context,
        services=services,
        customer_name=customer_name,
    )


async def _offer_dates(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    *,
    services: Sequence[BookingOption] | None = None,
    customer_name: str | None = None,
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
            return _handoff_for_reason(plan.handoff_reason)
        dates = _snapshot_options(
            await port.list_dates(
                inbound.business_id,
                service_id,
                requirements,
            )
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
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

    estimate = _estimate_message(plan)
    if len(dates) > 10:
        body = (
            f"{estimate}\n\nTenho disponibilidade em vários dias. "
            f"{customer_lead(customer_name)}qual dia da semana fica melhor "
            "para o seu atendimento?"
        )
        return _transition(
            ConversationState.BOOKING_WEEKDAY,
            _intake_context(context),
            weekday_selection_message(
                weekday_options(dates),
                body=body,
            ),
        )

    body = (
        f"{estimate}\n\nTenho disponibilidade nestas datas. "
        f"{customer_lead(customer_name)}qual dia fica melhor "
        "para o seu atendimento?"
    )
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(dates, body=body),
    )


async def _offer_times_for_date(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    service_id: uuid.UUID,
    selected_date: str,
    requirements: BookingRequirements,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    try:
        times = _snapshot_options(
            await port.list_times(
                inbound.business_id,
                service_id,
                selected_date,
                requirements,
            )
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))
    if not times:
        return await _return_to_dates(
            inbound,
            port,
            service_id,
            context,
            requirements,
            customer_name=customer_name,
        )

    selected_time = _time_from_text(inbound.body, times)
    if selected_time is not None:
        return await _booking_confirm_transition(
            inbound,
            port,
            context,
            service_id,
            selected_date,
            selected_time,
            requirements,
            customer_name=customer_name,
        )
    return _transition(
        ConversationState.BOOKING_TIME,
        {**_intake_context(context), "selected_date": selected_date},
        time_selection_message(
            times,
            body=_time_prompt(selected_date, times, customer_name),
        ),
    )


async def _booking_confirm_transition(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    service_id: uuid.UUID,
    selected_date: str,
    selected_time: str,
    requirements: BookingRequirements,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    candidate = {
        "service_id": str(service_id),
        "selected_date": selected_date,
        "selected_time": selected_time,
    }
    updated = {
        **_intake_context(context),
        **candidate,
        "candidate_booking": candidate,
    }
    body = await _confirmation_body(
        inbound,
        port,
        updated,
        service_id,
        selected_date,
        selected_time,
        requirements,
        customer_name=customer_name,
    )
    return _transition(
        ConversationState.BOOKING_CONFIRM,
        updated,
        booking_confirmation_message(body),
    )


async def _confirmation_body(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    service_id: uuid.UUID,
    selected_date: str,
    selected_time: str,
    requirements: BookingRequirements,
    *,
    customer_name: str | None = None,
) -> str:
    services = _snapshot_options(await port.list_services(inbound.business_id))
    service = next(
        (item for item in services if item.id == str(service_id)),
        None,
    )
    service_label = service.label if service is not None else "Serviço selecionado"
    lines = [
        (
            f"Perfeito, {customer_name}. Só para confirmar:"
            if customer_name
            else "Perfeito. Só para confirmar:"
        ),
        f"Serviço: {service_label}",
    ]
    if requirements.quantity > 1:
        lines.append(f"Quantidade: {requirements.quantity}")
    lines.extend(
        (
            f"Data: {date_short_label(selected_date)}",
            f"Horário: {selected_time}",
        )
    )
    address = ServiceAddress.from_snapshot(context.get("service_address"))
    if address is not None:
        lines.append(f"Endereço: {address.address_line}")
    try:
        plan = await port.estimate(
            inbound.business_id,
            service_id,
            requirements,
        )
    except BookingRequiresHandoff:
        plan = None
    if plan is not None:
        if plan.service.estimated_price is not None:
            prefix = "Valor" if plan.service.pricing_type is PricingType.FIXED else "Valor estimado"
            lines.append(f"{prefix}: {_format_brl(plan.service.estimated_price)}")
        lines.append(
            "Duração estimada: "
            f"{_format_duration(plan.service.estimated_duration_minutes)}"
        )
    lines.append("Posso confirmar?")
    return "\n".join(lines)[:1024]


def _time_prompt(
    selected_date: str,
    times: Sequence[BookingOption],
    customer_name: str | None,
) -> str:
    lead = customer_lead(customer_name)
    if not times:
        return f"{lead}não encontrei horários disponíveis nessa data."
    if len(times) == 1:
        return (
            f"{lead}para {date_short_label(selected_date)}, tenho "
            f"{times[0].label} disponível. Esse horário funciona para você?"
        )
    first = times[0].label
    last = times[-1].label
    return (
        f"{lead}para {date_short_label(selected_date)}, tenho horários disponíveis "
        f"entre {first} e {last}. Qual fica melhor para o seu atendimento?"
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
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    dates = _snapshot_options(
        await port.list_dates(inbound.business_id, service_id, requirements)
    )
    if not dates:
        return await _restart_service_selection(inbound, port)
    if len(dates) > 10:
        return _transition(
            ConversationState.BOOKING_WEEKDAY,
            _intake_context(context),
            weekday_selection_message(
                weekday_options(dates),
                body=(
                    "Não há horários disponíveis na data escolhida. "
                    f"{customer_lead(customer_name)}qual outro dia da semana "
                    "fica melhor?"
                ),
            ),
        )
    return _transition(
        ConversationState.BOOKING_DATE,
        _intake_context(context),
        date_selection_message(
            dates,
            body=(
                "Não há horários disponíveis na data escolhida. "
                f"{customer_lead(customer_name)}qual outra data fica melhor?"
            ),
        ),
    )


def _transition(
    state: ConversationState,
    context: dict[str, Any],
    outbound: OutboundMessage,
    *,
    automation_enabled: bool = True,
    handoff_status: str = "none",
    follow_ups: tuple[OutboundMessage, ...] = (),
) -> ConversationTransition:
    return ConversationTransition(
        state=state,
        context=context,
        automation_enabled=automation_enabled,
        handoff_status=handoff_status,
        outbound=outbound,
        follow_ups=follow_ups,
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
    padded = f" {normalized} "

    exact_matches: list[str] = []
    for option in dates:
        parsed = date.fromisoformat(option.id)
        label = normalize_portuguese(option.label)
        day_month = normalize_portuguese(parsed.strftime("%d/%m"))
        day_number = str(parsed.day)
        if (
            f" {label} " in padded
            or f" {day_month} " in padded
            or re.search(rf"\bdia\s+0?{parsed.day}\b", normalized)
        ):
            exact_matches.append(option.id)
            continue
        if re.search(rf"\b0?{parsed.day}\b", normalized):
            exact_matches.append(option.id)
    if len(set(exact_matches)) == 1:
        return exact_matches[0]

    relative_days = {
        "amanha": 1,
        "depois de amanha": 2,
    }
    today = date.today()
    for phrase, offset in relative_days.items():
        if f" {phrase} " in padded:
            target = today.toordinal() + offset
            match = next(
                (
                    option.id
                    for option in dates
                    if date.fromisoformat(option.id).toordinal() == target
                ),
                None,
            )
            if match is not None:
                return match

    weekday = weekday_from_text(body)
    if weekday is None:
        return None
    weekday_matches = dates_for_weekday(dates, weekday)
    return weekday_matches[0].id if len(weekday_matches) == 1 else None


def _time_from_text(
    body: str | None,
    times: Sequence[BookingOption],
) -> str | None:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return None
    padded = f" {normalized} "
    hour_words = {
        0: ("meia noite",),
        1: ("uma",),
        2: ("duas",),
        3: ("tres",),
        4: ("quatro",),
        5: ("cinco",),
        6: ("seis",),
        7: ("sete",),
        8: ("oito",),
        9: ("nove",),
        10: ("dez",),
        11: ("onze",),
        12: ("doze", "meio dia"),
        13: ("treze", "uma da tarde"),
        14: ("quatorze", "duas da tarde"),
        15: ("quinze", "tres da tarde"),
        16: ("dezesseis", "quatro da tarde"),
        17: ("dezessete", "cinco da tarde"),
        18: ("dezoito", "seis da tarde"),
        19: ("dezenove", "sete da noite"),
        20: ("vinte", "oito da noite"),
        21: ("vinte e uma", "nove da noite"),
        22: ("vinte e duas", "dez da noite"),
        23: ("vinte e tres", "onze da noite"),
    }
    for option in times:
        try:
            hour_text, minute_text = option.label.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (ValueError, AttributeError):
            continue
        label_phrase = normalize_portuguese(option.label)
        candidates = {
            label_phrase,
            str(hour),
            f"{hour}h",
        }
        if minute:
            candidates.update(
                {
                    f"{hour} {minute:02d}",
                    f"{hour}h{minute:02d}",
                    f"{hour} e {minute}",
                }
            )
        elif hour in hour_words:
            candidates.update(hour_words[hour])
        if any(f" {normalize_portuguese(candidate)} " in padded for candidate in candidates):
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


def _selected_weekday(action: str | None) -> int | None:
    if action is None or not action.startswith("weekday:"):
        return None
    raw = action.removeprefix("weekday:")
    try:
        weekday = int(raw)
    except ValueError:
        return None
    return weekday if 0 <= weekday <= 6 else None


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
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return value if 1 <= value <= 999 else None

    normalized = normalize_portuguese(body or "")
    match = re.search(r"\b([1-9]\d{0,2})\b", normalized)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 999 else None
    words = {
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "quatro": 4,
        "cinco": 5,
        "seis": 6,
        "sete": 7,
        "oito": 8,
        "nove": 9,
        "dez": 10,
    }
    for token, value in words.items():
        if f" {token} " in f" {normalized} ":
            return value
    return None


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
        return "Já tenho as informações necessárias para consultar a agenda."
    formatted = _format_brl(price)
    if plan.service.pricing_type is PricingType.FIXED:
        return f"O valor do serviço é {formatted}."
    return (
        f"Pelas informações que você passou, o valor estimado é {formatted}. "
        "Ele pode mudar se houver uma condição diferente no local."
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


def _handoff_for_reason(reason: str | None) -> ConversationTransition:
    normalized = normalize_portuguese(reason or "")
    if "travel estimate unavailable" in normalized or "travel_estimate_unavailable" in normalized:
        body = (
            "Consegui avançar com os dados do atendimento, mas preciso confirmar "
            "o deslocamento até esse endereço antes de fechar o horário. "
            "Vou chamar uma pessoa da equipe para continuar com você."
        )
    elif "outside service area" in normalized or "address_outside_service_area" in normalized:
        body = (
            "O endereço precisa de uma confirmação da equipe antes de eu conseguir "
            "fechar o agendamento. Vou encaminhar o atendimento para continuarem com você."
        )
    elif "quote" in normalized or "orcamento" in normalized:
        body = (
            "Para não te passar um valor incorreto, essa etapa precisa de uma "
            "conferência da equipe. Vou encaminhar o atendimento para continuarem com você."
        )
    else:
        body = (
            "Cheguei a uma etapa que precisa de uma conferência da equipe para "
            "seguir com segurança. Vou encaminhar o atendimento para continuarem com você."
        )
    return _handoff_transition(body)


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

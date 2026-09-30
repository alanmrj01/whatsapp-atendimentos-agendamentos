from __future__ import annotations

import uuid
import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.booking.equipment_recommender import (
    recommend_equipment,
    required_capacity_btu,
)
from app.booking.domain import (
    AccessCondition,
    BookingPlan,
    BookingRequirements,
    PricingType,
    ServiceAddress,
    ServiceIntake,
)

from app.conversations.facts import (
    enrich_context_from_message,
    missing_equipment_profile_fields,
    parse_number_answer,
)
from app.conversations.context_policy import (
    context_for_service_change,
    invalidate_changed_facts,
)
from app.conversations.constants import (
    ALLOWED_CONTEXT_KEYS,
    ACCESS_DIFFICULT,
    ACCESS_NORMAL,
    ACCESS_UNKNOWN,
    ADDRESS_CITY_CONFIRM,
    ADDRESS_CITY_OTHER,
    ATTENDEE_CUSTOMER,
    ATTENDEE_OTHER,
    EQUIPMENT_BOTH,
    EQUIPMENT_HAS,
    EQUIPMENT_INSTALLATION,
    EQUIPMENT_MODEL_KNOWN,
    EQUIPMENT_MODEL_RECOMMEND,
    EQUIPMENT_NEEDS,
    EQUIPMENT_PREF_COST_BENEFIT,
    EQUIPMENT_PREF_ECONOMY,
    EQUIPMENT_PREF_MODERN,
    EQUIPMENT_CYCLE_COLD,
    EQUIPMENT_CYCLE_HEAT_COOL,
    EQUIPMENT_SPACE_NO_LIMIT,
    EQUIPMENT_DELIVERY_PICKUP,
    EQUIPMENT_DELIVERY_ADDRESS,
    EQUIPMENT_DELIVERY_WITH_INSTALLATION,
    EQUIPMENT_INSTALLATION_SAME_ADDRESS,
    EQUIPMENT_INSTALLATION_OTHER_ADDRESS,
    EQUIPMENT_PURCHASE,
    CHANGE_CONFIRM,
    CHANGE_KEEP,
    MEDIA_HANDOFF,
    MEDIA_CONTINUE_TEXT,
    HEIGHT_AT_MOST_3M,
    HEIGHT_OVER_3M,
    PHONE_CONFIRM,
    PHONE_OTHER,
    PROPERTY_BUILDING,
    PROPERTY_CONDOMINIUM,
    PROPERTY_HOUSE,
    QUOTE_FINISH,
    QUOTE_SCHEDULE,
    BOOKING_BACK,
    BOOKING_CANCEL,
    BOOKING_CONFIRM,
    POST_BOOKING_HELP_YES,
    POST_BOOKING_HELP_NO,
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
    attendee_message,
    attendee_name_message,
    building_hours_message,
    equipment_model_known_message,
    equipment_model_request_message,
    equipment_preference_message,
    equipment_profile_intro_message,
    equipment_profile_message,
    equipment_cycle_message,
    equipment_space_message,
    equipment_image_message,
    equipment_delivery_message,
    delivery_installation_address_message,
    change_confirmation_message,
    additional_request_message,
    equipment_photo_request_message,
    diagnostic_noise_video_request_message,
    media_received_message,
    post_booking_help_message,
    farewell_message,
    unsupported_media_message,
    equipment_purchase_clarification_message,
    existing_booking_selection_message,
    gate_details_message,
    installation_equipment_status_message,
    installation_height_message,
    handoff_message,
    main_menu_message,
    name_request_message,
    no_services_message,
    phone_confirmation_message,
    phone_request_message,
    property_type_message,
    quantity_selection_message,
    quote_decision_message,
    reaction_message,
    reschedule_completed_message,
    reschedule_confirmation_message,
    reschedule_message,
    service_selection_message,
    site_limit_message,
    slot_unavailable_message,
    tubing_confirmation_message,
    tubing_guidance_message,
    tubing_length_message,
    tubing_variation_message,
    time_selection_message,
    weekday_selection_message,
)
from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
    Interpretation,
    correction_focus,
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
    interpreter = DeterministicConversationInterpreter()
    interpretation = interpreter.interpret(inbound.body)
    action = inbound.interactive_id
    base_context = _clean_context(conversation.context)

    commercial_handoff = await _commercial_negotiation_handoff_if_applicable(
        conversation,
        inbound,
        state,
        base_context,
        interpretation,
        booking_port,
        action,
    )
    if commercial_handoff is not None:
        return commercial_handoff

    catalog_guard = await _purchase_catalog_guard_if_needed(
        conversation,
        inbound,
        state,
        base_context,
        interpretation,
        booking_port,
        action,
    )
    if catalog_guard is not None:
        return catalog_guard

    pending_state_answer = _message_can_answer_pending_state(
        state,
        inbound,
        base_context,
        interpretation,
    )
    question_without_slot_answer = (
        state not in {
            ConversationState.START,
            ConversationState.MENU,
            ConversationState.COMPLETED,
            ConversationState.HUMAN_HANDOFF,
            ConversationState.POST_BOOKING_HELP,
        }
        and action is None
        and inbound.message_type == "text"
        and isinstance(inbound.body, str)
        and inbound.body.strip()
        and not pending_state_answer
        and (
            interpretation.has_act(ConversationAct.SIDE_QUESTION)
            or _looks_like_parallel_digression(inbound.body)
            or _equipment_technical_question(
                normalize_portuguese(inbound.body)
            ) is not None
        )
    )
    context = (
        enrich_context_from_message(
            base_context,
            None,
            whatsapp_id=inbound.whatsapp_id,
        )
        if action is not None or question_without_slot_answer
        else enrich_context_from_message(
            base_context,
            inbound.body,
            whatsapp_id=inbound.whatsapp_id,
        )
    )
    budget_updates = {
        key: value
        for key, value in (
            ("equipment_budget_max", interpretation.equipment_budget_max),
            ("service_budget_max", interpretation.service_budget_max),
            ("total_budget_max", interpretation.total_budget_max),
        )
        if value is not None
    }
    if action is None and budget_updates:
        context = invalidate_changed_facts(
            base_context,
            {**context, **budget_updates},
        )

    if action == MEDIA_HANDOFF and context.get("media_handoff_pending") is True:
        return _handoff_transition(
            "Vou encaminhar você para uma pessoa da equipe. "
            "Como o atendimento será manual, a resposta e o agendamento podem levar um pouco mais."
        )
    if action == MEDIA_CONTINUE_TEXT and context.get("media_handoff_pending") is True:
        updated = dict(context)
        updated.pop("media_handoff_pending", None)
        if state not in {
            ConversationState.START,
            ConversationState.MENU,
            ConversationState.HUMAN_HANDOFF,
            ConversationState.POST_BOOKING_HELP,
        }:
            return await _resume_pending_question(
                conversation,
                inbound,
                updated,
                booking_port,
                prefix="Tudo bem. Seguimos por texto.",
            )
        return _transition(
            state,
            updated,
            _text_message("Tudo bem. Seguimos por texto. Como posso te ajudar?"),
        )
    if (
        context.get("media_handoff_pending") is True
        and inbound.message_type == "text"
        and action is None
        and isinstance(inbound.body, str)
        and inbound.body.strip()
    ):
        context = dict(context)
        context.pop("media_handoff_pending", None)

    if (
        inbound.message_type in {"image", "audio", "video"}
        and state is not ConversationState.BOOKING_EQUIPMENT_MODEL
    ):
        return _transition(
            state,
            {**context, "media_handoff_pending": True},
            unsupported_media_message(inbound.message_type),
        )

    if action in {CHANGE_CONFIRM, CHANGE_KEEP} and (
        _context_string(context, "pending_change_action") is not None
        or _context_string(context, "pending_service_change_id") is not None
    ):
        return await _handle_pending_change(
            conversation,
            inbound,
            context,
            action,
            booking_port,
        )

    if interpretation.intent is ConversationIntent.HUMAN_HANDOFF:
        return _handoff_transition(conversation.handoff_message)

    paused_quote_transition = await _resume_paused_quote_if_requested(
        conversation,
        inbound,
        state,
        context,
        interpretation,
        action,
        booking_port,
    )
    if paused_quote_transition is not None:
        return paused_quote_transition

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
        if interpretation.has_act(ConversationAct.SOCIAL) or interpretation.has_act(
            ConversationAct.NEGATED_ACTION
        ):
            return _transition(
                state,
                context,
                name_request_message(
                    "Tudo certo. Antes de continuar, como você gostaria de ser chamado?"
                ),
            )
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
            pending: dict[str, Any] = dict(context)
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

    suggestion = context.get("equipment_suggestion")
    normalized_turn = normalize_portuguese(inbound.body or "")
    if (
        isinstance(suggestion, dict)
        and action is None
        and normalized_turn in {
            "quero esse",
            "quero esse mesmo",
            "esse",
            "esse mesmo",
            "pode ser esse",
            "fico com esse",
            "vou ficar com esse",
            "troca por esse",
            "quero trocar por esse",
        }
    ):
        updated = {
            **context,
            "recommended_equipment": dict(suggestion),
            "recommendation_presented": True,
        }
        updated.pop("equipment_suggestion", None)
        updated.pop("quote_presented", None)
        label = suggestion.get("label")
        confirmation_text = (
            f"Perfeito. Atualizei sua escolha para {label}."
            if isinstance(label, str) and label
            else "Perfeito. Atualizei sua escolha de equipamento."
        )
        if state in {
            ConversationState.COMPLETED,
            ConversationState.POST_BOOKING_HELP,
        }:
            return _transition(
                ConversationState.POST_BOOKING_HELP,
                updated,
                _text_message(confirmation_text),
                follow_ups=(post_booking_help_message(),),
            )
        return await _resume_pending_question(
            conversation,
            inbound,
            updated,
            booking_port,
            prefix=confirmation_text,
        )

    if interpretation.intent is ConversationIntent.RESCHEDULE and state is not ConversationState.RESCHEDULE:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="reschedule")
    if interpretation.intent is ConversationIntent.CANCEL and state is not ConversationState.CANCEL:
        return await _begin_existing_booking_flow(inbound, booking_port, purpose="cancel")

    pending_state_answer = _message_can_answer_pending_state(
        state,
        inbound,
        context,
        interpretation,
    )
    equipment_question_answer = (
        None
        if (
            action in {QUOTE_FINISH, QUOTE_SCHEDULE}
            or (
                state is ConversationState.BOOKING_EQUIPMENT_MODEL
                and pending_state_answer
            )
        )
        else await _equipment_question_answer(
            inbound,
            context,
            interpretation,
            booking_port,
        )
    )
    question_answer = (
        equipment_question_answer
        or (
            None
            if action in {QUOTE_FINISH, QUOTE_SCHEDULE}
            else await _service_question_answer(
                inbound,
                context,
                interpretation,
                booking_port,
            )
        )
    )
    if (
        question_answer is not None
        and state is ConversationState.COMPLETED
        and not interpretation.has(ConversationIntent.BOOK)
        and not interpretation.has(ConversationIntent.AVAILABILITY)
    ):
        return _transition(
            ConversationState.POST_BOOKING_HELP,
            context,
            _text_message(question_answer),
            follow_ups=(post_booking_help_message(),),
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
        and not interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
        and context.get("request_mode") != "quote"
    ):
        follow_up = (
            f"{question_answer}\n\n"
            f"Se quiser, {customer_lead(customer_name)}posso continuar com o atendimento."
        )
        return _transition(
            ConversationState.MENU,
            context,
            _text_message(follow_up),
        )

    if state is ConversationState.POST_BOOKING_HELP and question_answer is not None:
        return _transition(
            ConversationState.POST_BOOKING_HELP,
            context,
            _text_message(question_answer),
            follow_ups=(post_booking_help_message(),),
        )

    booking_states = {
        ConversationState.CUSTOMER_NAME,
        ConversationState.BOOKING_QUANTITY,
        ConversationState.BOOKING_SERVICE,
        ConversationState.BOOKING_ACCESS,
        ConversationState.BOOKING_ADDRESS,
        ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        ConversationState.BOOKING_EQUIPMENT_MODEL,
        ConversationState.BOOKING_EQUIPMENT_PROFILE,
        ConversationState.BOOKING_EQUIPMENT_DELIVERY,
        ConversationState.BOOKING_INSTALLATION_HEIGHT,
        ConversationState.BOOKING_PROPERTY,
        ConversationState.BOOKING_BUILDING_HOURS,
        ConversationState.BOOKING_GATE_DETAILS,
        ConversationState.BOOKING_TUBING,
        ConversationState.BOOKING_SITE_LIMIT,
        ConversationState.BOOKING_WEEKDAY,
        ConversationState.BOOKING_DATE,
        ConversationState.BOOKING_TIME,
        ConversationState.BOOKING_ATTENDEE,
        ConversationState.BOOKING_ATTENDEE_NAME,
        ConversationState.BOOKING_PHONE_CONFIRM,
        ConversationState.BOOKING_CONFIRM,
        ConversationState.QUOTE_DECISION,
    }
    if state in booking_states and interpretation.has_act(ConversationAct.SOCIAL):
        return await _resume_pending_question(
            conversation,
            inbound,
            context,
            booking_port,
            prefix="Tudo certo.",
        )
    if state in booking_states and interpretation.has_act(
        ConversationAct.NEGATED_ACTION
    ):
        if (
            interpretation.has(ConversationIntent.SERVICE_INTENT)
            and interpretation.service_key == "split-installation"
            and "nao quero comprar" in interpretation.normalized_text
        ):
            return await _handle_service(
                inbound,
                context,
                EQUIPMENT_INSTALLATION,
                booking_port,
                interpretation=interpretation,
                customer_name=customer_name,
            )
        return await _resume_pending_question(
            conversation,
            inbound,
            context,
            booking_port,
            prefix="Tudo bem, seguimos sem essa alteração.",
        )
    if (
        state in booking_states
        and question_answer is not None
        and (
            interpretation.has_act(ConversationAct.SIDE_QUESTION)
            or equipment_question_answer is not None
        )
        and not pending_state_answer
    ):
        return await _resume_pending_question(
            conversation,
            inbound,
            context,
            booking_port,
            prefix=question_answer,
        )
    if (
        state in booking_states
        and action is None
        and interpretation.intent in {
            ConversationIntent.SERVICE_INTENT,
            ConversationIntent.EQUIPMENT_PURCHASE,
        }
        and not _has_service_question(interpretation)
    ):
        if interpretation.intent is ConversationIntent.EQUIPMENT_PURCHASE:
            # Durante uma cotação de instalação já em andamento, frases como
            # "quero cotar aparelho" são respostas ao fluxo atual. Reiniciar o
            # serviço aqui apaga endereço e escolhas já coletadas e provoca
            # perguntas repetidas.
            if not (
                _context_service_id(context) is not None
                and context.get("request_mode") == "quote"
            ):
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
            and current_id is not None
            and matched.id != str(current_id)
        ):
            updated = {
                **context,
                "pending_service_change_id": matched.id,
                "pending_service_change_label": matched.label,
            }
            prompt = (
                additional_request_message(matched.label)
                if interpretation.has_act(ConversationAct.ADDITIONAL_REQUEST)
                else change_confirmation_message(matched.label)
            )
            return _transition(
                state,
                updated,
                prompt,
            )
        if matched is not None and current_id is None:
            return await _handle_service(
                inbound,
                {},
                f"service:{matched.id}",
                booking_port,
                interpretation=interpretation,
                fallback_message=conversation.fallback_message,
                customer_name=customer_name,
            )

    if (
        state in booking_states
        and action is None
        and inbound.message_type == "text"
        and isinstance(inbound.body, str)
        and inbound.body.strip()
        and interpretation.intent is ConversationIntent.UNKNOWN
        and not pending_state_answer
        and _looks_like_parallel_digression(inbound.body)
    ):
        return await _resume_pending_question(
            conversation,
            inbound,
            context,
            booking_port,
            prefix=_closed_loop_digression_reply(inbound.body),
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
    elif (
        state is ConversationState.COMPLETED
        and _is_completion_acknowledgement(inbound.body)
    ):
        transition = _transition(
            ConversationState.COMPLETED,
            context,
            reaction_message(inbound.provider_message_id),
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
        retained_budget = {
            key: context[key]
            for key in (
                "equipment_budget_max",
                "service_budget_max",
                "total_budget_max",
            )
            if key in context
        }
        if retained_budget:
            transition = replace(
                transition,
                context={**transition.context, **retained_budget},
            )
    elif state is ConversationState.MENU:
        transition = await _handle_menu(
            inbound,
            action,
            booking_port,
            context=context,
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
    elif state is ConversationState.BOOKING_EQUIPMENT_OWNERSHIP:
        transition = await _handle_equipment_ownership(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_EQUIPMENT_MODEL:
        transition = await _handle_equipment_model(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_EQUIPMENT_PROFILE:
        transition = await _handle_equipment_profile(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_EQUIPMENT_DELIVERY:
        transition = await _handle_equipment_delivery(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_INSTALLATION_HEIGHT:
        transition = await _handle_installation_height(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_PROPERTY:
        transition = await _handle_property_type(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_BUILDING_HOURS:
        transition = await _handle_building_hours(
            inbound, context, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_GATE_DETAILS:
        transition = await _handle_gate_details(
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
    elif state is ConversationState.BOOKING_ATTENDEE:
        transition = await _handle_attendee(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_ATTENDEE_NAME:
        transition = await _handle_attendee_name(
            inbound, context, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_PHONE_CONFIRM:
        transition = await _handle_phone_confirmation(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.QUOTE_DECISION:
        transition = await _handle_quote_decision(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.BOOKING_CONFIRM:
        transition = await _handle_confirmation(
            inbound, context, action, booking_port, customer_name=customer_name
        )
    elif state is ConversationState.POST_BOOKING_HELP:
        transition = await _handle_post_booking_help(
            conversation,
            inbound,
            interpretation,
            action,
            booking_port,
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
                "Não consegui confirmar seu nome com segurança. "
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
        resumed_context = {
            key: value
            for key, value in context.items()
            if key not in {"pending_customer_message", "pending_interactive_id"}
        }
        resumed = replace(
            conversation,
            state=(
                ConversationState.MENU.value
                if pending_action is not None and pending_action.startswith("menu.")
                else ConversationState.START.value
            ),
            context=resumed_context,
            customer_name=customer_name,
        )
        transition = await _route_named_conversation(
            resumed,
            replay,
            resumed_context,
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
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.RESCHEDULE_QUESTION,
        )
    )


def _looks_like_commercial_negotiation(
    body: str | None,
    *,
    state: ConversationState,
    context: dict[str, Any],
    interpretation: Interpretation,
) -> bool:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return False

    negotiation_phrases = (
        "mais barato",
        "mais em conta",
        "valor menor",
        "preco menor",
        "melhor preco",
        "melhorar o valor",
        "melhorar esse valor",
        "melhorar esse preco",
        "tem como melhorar",
        "consegue melhorar",
        "da para melhorar",
        "dar desconto",
        "tem desconto",
        "consegue desconto",
        "abaixar o valor",
        "baixar o valor",
        "reduzir o valor",
        "reduzir o preco",
        "negociar o valor",
        "negociar o preco",
        "ficou caro",
        "esta caro",
        "ta caro",
        "muito caro",
        "achei caro",
        "passou do meu orcamento",
        "fora do meu orcamento",
        "nao cabe no meu orcamento",
        "nao consigo pagar",
        "consegue fazer por",
        "da para fazer por",
        "tem como fazer por",
    )
    if any(phrase in normalized for phrase in negotiation_phrases):
        return True

    price_already_presented = (
        state in {
            ConversationState.BOOKING_CONFIRM,
            ConversationState.QUOTE_DECISION,
        }
        or context.get("quote_presented") is True
        or context.get("recommendation_presented") is True
    )
    return (
        price_already_presented
        and interpretation.has(ConversationIntent.PRICE_QUESTION)
        and any(
            token in normalized
            for token in ("preco", "valor", "quanto", "orcamento", "custa")
        )
    )


async def _is_ac_purchase_or_installation_context(
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> bool:
    if context.get("purchase_mode") in {"purchase", "both"}:
        return True
    if context.get("purchase_only") is True:
        return True
    if context.get("equipment_ownership") in {"needs_equipment", "has_equipment"}:
        return True
    if isinstance(context.get("recommended_equipment"), dict):
        return True
    if interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE):
        return True
    if (
        interpretation.has(ConversationIntent.SERVICE_INTENT)
        and interpretation.service_key == "split-installation"
    ):
        return True

    service_id = _context_service_id(context)
    if service_id is None:
        return False
    try:
        port = _require_booking_port(booking_port)
        services = _snapshot_options(await port.list_services(inbound.business_id))
    except (BookingPortUnavailable, BookingRequiresHandoff):
        return False
    return _service_kind(services, service_id) == "installation"


async def _commercial_negotiation_handoff_if_applicable(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    state: ConversationState,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
    action: str | None,
) -> ConversationTransition | None:
    # Interactive navigation (confirmar/voltar/cancelar) must keep its normal
    # deterministic path. Commercial negotiation is recognized from free text.
    if action is not None:
        return None
    if not _looks_like_commercial_negotiation(
        inbound.body,
        state=state,
        context=context,
        interpretation=interpretation,
    ):
        return None
    if not await _is_ac_purchase_or_installation_context(
        inbound,
        context,
        interpretation,
        booking_port,
    ):
        return None

    body = (
        "Para compra ou instalação de ar-condicionado, podemos verificar um aparelho "
        "seminovo, conforme disponibilidade. Vou encaminhar à equipe para confirmar "
        "opções e valores."
    )
    return _transition(
        ConversationState.HUMAN_HANDOFF,
        _clean_context(context),
        _text_message(body),
        automation_enabled=False,
        handoff_status="waiting",
    )


def _looks_like_parallel_digression(body: str | None) -> bool:
    raw = " ".join((body or "").strip().split())
    normalized = normalize_portuguese(raw)
    if not normalized:
        return False
    if "?" in raw:
        return True
    question_leads = (
        "como ",
        "qual ",
        "quais ",
        "quanto ",
        "quando ",
        "onde ",
        "por que ",
        "porque ",
        "posso ",
        "pode ",
        "podem ",
        "voces ",
        "tem como ",
        "sera que ",
        "existe ",
    )
    if any(normalized.startswith(lead.strip()) for lead in question_leads):
        return True
    comment_markers = (
        "so comentando",
        "so queria comentar",
        "so queria dizer",
        "aproveitando",
        "uma observacao",
        "acho isso",
        "na minha opiniao",
    )
    return any(marker in normalized for marker in comment_markers)


def _closed_loop_digression_reply(body: str | None) -> str:
    normalized = normalize_portuguese(body or "")
    question_markers = (
        "como ",
        "qual ",
        "quais ",
        "quanto ",
        "quando ",
        "onde ",
        "porque ",
        "por que ",
        "posso ",
        "pode ",
        "tem como ",
        "sera que ",
    )
    if (body and "?" in body) or any(
        normalized.startswith(marker.strip())
        for marker in question_markers
    ):
        return (
            "Essa dúvida não está descrita com segurança nos dados que tenho aqui, "
            "então prefiro não inventar uma resposta. Vou manter o ponto em que "
            "estávamos para não fazer você repetir informações."
        )
    return (
        "Entendi. Vou manter o ponto em que estávamos para não fazer você repetir "
        "informações nem reiniciar o atendimento."
    )


def _purchase_equipment_context(
    context: dict[str, Any],
    interpretation: Interpretation,
) -> bool:
    return (
        context.get("purchase_mode") in {"purchase", "both"}
        or context.get("purchase_only") is True
        or context.get("equipment_ownership") == "needs_equipment"
        or interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
    )


def _catalog_candidates_for_customer_request(
    catalog: Sequence[Any],
    body: str | None,
    *,
    force_model_answer: bool,
) -> tuple[bool, list[Any]]:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return False, list(catalog)

    canonical = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    capacity_match = re.search(r"\b(\d{4,5})\s*btu\b", normalized)
    requested_capacity = int(capacity_match.group(1)) if capacity_match else None

    brand_matches = [
        item
        for item in catalog
        if re.sub(
            r"[^a-z0-9]+",
            " ",
            normalize_portuguese(getattr(item, "brand", "")),
        ).strip()
        in canonical
    ]
    line_matches = [
        item
        for item in catalog
        if (
            len(
                re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    normalize_portuguese(getattr(item, "line", "")),
                ).strip()
            )
            >= 4
            and re.sub(
                r"[^a-z0-9]+",
                " ",
                normalize_portuguese(getattr(item, "line", "")),
            ).strip()
            in canonical
        )
    ]

    explicit = bool(
        requested_capacity is not None
        or brand_matches
        or line_matches
        or "modelo" in normalized
    )
    if force_model_answer:
        explicit = True
    if not explicit:
        return False, list(catalog)

    candidates = list(catalog)
    if brand_matches:
        allowed = {getattr(item, "item_id", None) for item in brand_matches}
        candidates = [
            item for item in candidates
            if getattr(item, "item_id", None) in allowed
        ]
    if line_matches:
        allowed = {getattr(item, "item_id", None) for item in line_matches}
        candidates = [
            item for item in candidates
            if getattr(item, "item_id", None) in allowed
        ]
    if force_model_answer and not brand_matches and not line_matches and requested_capacity is None:
        candidates = []
    if requested_capacity is not None:
        candidates = [
            item
            for item in candidates
            if getattr(item, "capacity_btu", None) == requested_capacity
        ]

    if "quente frio" in normalized or "quente e frio" in normalized:
        candidates = [
            item
            for item in candidates
            if "heat_cool" in getattr(item, "cycles", ())
        ]
    elif "so frio" in normalized or "somente frio" in normalized:
        candidates = [
            item
            for item in candidates
            if "cold" in getattr(item, "cycles", ())
            and "heat_cool" not in getattr(item, "cycles", ())
        ]
    return True, candidates


async def _purchase_catalog_guard_if_needed(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    state: ConversationState,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
    action: str | None,
) -> ConversationTransition | None:
    if action is not None or inbound.message_type != "text":
        return None
    if not _purchase_equipment_context(context, interpretation):
        return None

    normalized = normalize_portuguese(inbound.body or "")
    if not normalized:
        return None
    if normalized in {
        "sim",
        "nao",
        "nao sei",
        "nao tenho",
        "nao conheco",
        "sem preferencia",
        "pode recomendar",
        "quero recomendacao",
    }:
        return None

    force_model_answer = state is ConversationState.BOOKING_EQUIPMENT_MODEL
    try:
        port = _require_booking_port(booking_port)
        catalog = tuple(await port.list_equipment_catalog(inbound.business_id))
    except (BookingPortUnavailable, BookingRequiresHandoff):
        return None
    if not catalog:
        return None

    explicit, candidates = _catalog_candidates_for_customer_request(
        catalog,
        inbound.body,
        force_model_answer=force_model_answer,
    )
    if not explicit or candidates:
        return None

    misses = context.get("catalog_miss_count")
    previous_misses = misses if isinstance(misses, int) else 0
    alternative_presented = context.get("catalog_alternative_presented") is True
    miss_count = previous_misses + 1 if alternative_presented or previous_misses == 0 else previous_misses
    updated = {
        **context,
        "catalog_miss_count": miss_count,
        "last_catalog_miss": " ".join((inbound.body or "").strip().split())[:180],
        "equipment_model_known": False,
    }
    for key in (
        "equipment_model",
        "recommended_equipment",
        "equipment_suggestion",
        "recommendation_presented",
        "quote_presented",
        "quote_paused",
    ):
        updated.pop(key, None)

    if alternative_presented and miss_count >= 2:
        return _transition(
            ConversationState.HUMAN_HANDOFF,
            _clean_context(updated),
            _text_message(
                "Essa configuração continua fora do catálogo ativo. Para não inventar "
                "um aparelho, vou encaminhar à equipe para verificar disponibilidade "
                "e alternativas."
            ),
            automation_enabled=False,
            handoff_status="waiting",
        )

    prefix = (
        "Esse aparelho ou configuração não aparece no catálogo ativo. Para não "
        "inventar um modelo nem assumir uma capacidade inadequada, vou dimensionar "
        "seu ambiente e te mostrar somente uma opção realmente cadastrada."
        if previous_misses == 0
        else
        "Essa configuração continua fora do catálogo ativo. Antes de oferecer uma "
        "alternativa segura, ainda preciso concluir o dimensionamento do ambiente."
    )
    missing = missing_equipment_profile_fields(updated)
    if missing:
        updated["equipment_profile_intro_sent"] = True
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_PROFILE,
            _clean_context(updated),
            _text_message(prefix),
            follow_ups=(_equipment_profile_prompt(missing),),
        )

    transition = await _handle_equipment_profile(
        replace(inbound, body=None),
        updated,
        None,
        booking_port,
        customer_name=conversation.customer_name,
    )
    return _prepend_transition_body(transition, prefix)


def _equipment_suggestion_snapshot(
    item: Any,
    *,
    required_btu: int | None,
    selected_cycle: str | None,
) -> dict[str, Any]:
    cycle = selected_cycle
    if cycle not in {"cold", "heat_cool"}:
        cycle = "heat_cool" if "heat_cool" in item.cycles else "cold"
    base_label = (
        f"{item.brand} {item.line} {item.capacity_btu:,} BTU"
        .replace(",", ".")
    )
    label = (
        f"{base_label} — Quente/Frio"
        if cycle == "heat_cool"
        else f"{base_label} — Só Frio"
    )
    return {
        "item_id": item.item_id,
        "label": label,
        "brand": item.brand,
        "line": item.line,
        "capacity_btu": item.capacity_btu,
        "preference": item.segment,
        "cycles": list(item.cycles),
        "selected_cycle": cycle,
        "features": list(item.features),
        "source_url": item.source_url,
        "image_url": item.image_url,
        "condenser_form": item.condenser_form,
        "indoor_dimensions_cm": item.indoor_dimensions_cm,
        "outdoor_dimensions_cm": item.outdoor_dimensions_cm,
        "voltage_v": getattr(item, "voltage_v", None),
        "voltage": getattr(item, "voltage", None),
        "model_sku": getattr(item, "model_sku", None),
        "wifi": getattr(item, "wifi", None),
        "inverter": getattr(item, "inverter", None),
        "price": item.price,
        "required_btu_reference": required_btu,
    }


def _equipment_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _equipment_display_label(item: Any) -> str:
    label = _equipment_value(item, "label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    brand = _equipment_value(item, "brand")
    line = _equipment_value(item, "line")
    capacity = _equipment_value(item, "capacity_btu")
    parts = [
        value.strip()
        for value in (brand, line)
        if isinstance(value, str) and value.strip()
    ]
    if isinstance(capacity, int) and not isinstance(capacity, bool):
        parts.append(f"{capacity:,} BTU".replace(",", "."))
    return " ".join(parts) or "esse equipamento"


def _equipment_technical_question(normalized: str) -> str | None:
    if any(term in normalized for term in ("voltagem", "tensao", "127v", "127 v", "220v", "220 v")):
        return "voltage"
    if any(
        term in normalized
        for term in (
            "dimensao",
            "dimensoes",
            "medida",
            "medidas",
            "largura",
            "profundidade",
            "tamanho do aparelho",
            "tamanho da evaporadora",
            "tamanho da condensadora",
        )
    ):
        return "dimensions"
    if any(term in normalized for term in ("sku", "codigo do modelo", "codigo do aparelho", "referencia do modelo")):
        return "sku"
    if any(term in normalized for term in ("quente frio", "quente e frio", "so frio", "ciclo")):
        return "cycle"
    if any(term in normalized for term in ("wifi", "wi fi", "alexa", "bluetooth", "inverter", "inteligencia artificial", " ia ")):
        return "feature"
    if any(term in normalized for term in ("garantia", "tempo de garantia")):
        return "warranty"
    if any(term in normalized for term in ("consumo", "kwh", "gasta muita energia", "economia de energia")):
        return "consumption"
    if any(term in normalized for term in ("gas refrigerante", "fluido refrigerante", "r32", "r410")):
        return "refrigerant"
    if any(term in normalized for term in ("nivel de ruido", "decibeis", "decibel", " db ")):
        return "noise"
    return None


def _format_dimensions(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    width = value.get("width")
    height = value.get("height")
    depth = value.get("depth")
    if not isinstance(width, (int, float)) or isinstance(width, bool):
        return None
    if not isinstance(height, (int, float)) or isinstance(height, bool):
        return None
    text = f"{float(width):g} × {float(height):g} cm (largura × altura)"
    if isinstance(depth, (int, float)) and not isinstance(depth, bool):
        text = (
            f"{float(width):g} × {float(height):g} × {float(depth):g} cm "
            "(largura × altura × profundidade)"
        )
    return text.replace(".", ",")


def _equipment_technical_answer(item: Any, normalized: str) -> str | None:
    kind = _equipment_technical_question(normalized)
    if kind is None:
        return None
    label = _equipment_display_label(item)

    if kind == "voltage":
        voltage = _equipment_value(item, "voltage")
        voltage_v = _equipment_value(item, "voltage_v")
        if isinstance(voltage, str) and voltage.strip():
            return f"A tensão cadastrada para {label} é {voltage.strip()}."
        if isinstance(voltage_v, int) and not isinstance(voltage_v, bool):
            return f"A tensão cadastrada para {label} é {voltage_v} V."
        return (
            f"A tensão de {label} não está cadastrada com segurança no catálogo. "
            "Prefiro não assumir esse dado."
        )

    if kind == "dimensions":
        indoor = _format_dimensions(_equipment_value(item, "indoor_dimensions_cm"))
        outdoor = _format_dimensions(_equipment_value(item, "outdoor_dimensions_cm"))
        if indoor and outdoor:
            return (
                f"As medidas cadastradas de {label} são: unidade interna {indoor}; "
                f"unidade externa {outdoor}."
            )
        if indoor:
            return f"A unidade interna de {label} tem medidas cadastradas de {indoor}."
        if outdoor:
            return f"A unidade externa de {label} tem medidas cadastradas de {outdoor}."
        return (
            f"As dimensões de {label} não estão cadastradas com segurança. "
            "Prefiro não inventar medidas."
        )

    if kind == "sku":
        sku = _equipment_value(item, "model_sku")
        if isinstance(sku, str) and sku.strip():
            return f"O código/modelo cadastrado de {label} é {sku.strip()}."
        return f"O código exato de {label} não está cadastrado no catálogo."

    if kind == "cycle":
        cycles = _equipment_value(item, "cycles")
        values = set(cycles) if isinstance(cycles, (list, tuple)) else set()
        if "heat_cool" in values:
            return f"{label} está cadastrado como quente/frio."
        if "cold" in values:
            return f"{label} está cadastrado como só frio."
        return f"O ciclo de {label} não está cadastrado com segurança."

    if kind == "feature":
        features = _equipment_value(item, "features")
        feature_values = tuple(features) if isinstance(features, (list, tuple)) else ()
        feature_text = " ".join(
            normalize_portuguese(str(value)) for value in feature_values
        )
        checks = (
            ("alexa", ("alexa", "amazon alexa")),
            ("bluetooth", ("bluetooth",)),
            ("Wi-Fi", ("wifi", "wi fi")),
            ("Inverter", ("inverter",)),
            ("IA", ("inteligencia artificial", " ia ")),
        )
        requested: list[tuple[str, bool]] = []
        for display, aliases in checks:
            if any(alias in f" {normalized} " for alias in aliases):
                explicit_flag = None
                if display == "Wi-Fi":
                    explicit_flag = _equipment_value(item, "wifi")
                elif display == "Inverter":
                    explicit_flag = _equipment_value(item, "inverter")
                present = (
                    explicit_flag
                    if isinstance(explicit_flag, bool)
                    else any(alias.strip() in feature_text for alias in aliases)
                    or (
                        display == "Inverter"
                        and "inverter" in normalize_portuguese(
                            str(_equipment_value(item, "line") or "")
                        )
                    )
                )
                requested.append((display, bool(present)))
        if requested:
            yes = [name for name, present in requested if present]
            no = [name for name, present in requested if not present]
            parts: list[str] = []
            if yes:
                parts.append(f"{label} tem " + ", ".join(yes) + " cadastrado.")
            if no:
                parts.append(
                    "Não tenho "
                    + ", ".join(no)
                    + f" cadastrado para {label}; prefiro não assumir compatibilidade."
                )
            return " ".join(parts)

    unsupported_labels = {
        "warranty": "a garantia",
        "consumption": "o consumo elétrico",
        "refrigerant": "o fluido refrigerante",
        "noise": "o nível de ruído em dB",
    }
    if kind in unsupported_labels:
        detail = unsupported_labels[kind]
        return (
            f"Não tenho {detail} de {label} cadastrado com segurança neste catálogo. "
            "Prefiro não inventar esse dado; a equipe pode confirmar a especificação exata."
        )
    return None


async def _equipment_question_answer(
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> str | None:
    normalized = normalize_portuguese(inbound.body or "")
    equipment_terms = (
        "ar condicionado",
        "aparelho",
        "equipamento",
        "modelo",
        "btu",
        "wifi",
        "alexa",
        "bluetooth",
        "inverter",
        "voltagem",
        "tensao",
        "dimensao",
        "dimensoes",
        "medida",
        "medidas",
        "sku",
        "codigo do modelo",
        "ciclo",
        "garantia",
        "consumo",
        "kwh",
        "fluido refrigerante",
        "gas refrigerante",
        "nivel de ruido",
        "decibeis",
    )
    cheaper_terms = ("mais barato", "mais em conta")
    has_equipment_context = (
        context.get("purchase_mode") in {"purchase", "both"}
        or context.get("purchase_only") is True
        or context.get("equipment_ownership") == "needs_equipment"
        or isinstance(context.get("recommended_equipment"), dict)
    )
    if not any(term in normalized for term in equipment_terms) and not (
        has_equipment_context
        and any(term in normalized for term in cheaper_terms)
    ):
        return None
    technical_terms = (
        "modelo",
        "btu",
        "wifi",
        "alexa",
        "bluetooth",
        "inverter",
        "voltagem",
        "tensao",
        "dimensao",
        "dimensoes",
        "medida",
        "medidas",
        "sku",
        "codigo do modelo",
        "ciclo",
        "garantia",
        "consumo",
        "kwh",
        "fluido refrigerante",
        "gas refrigerante",
        "nivel de ruido",
        "decibeis",
        "mais barato",
        "mais em conta",
    )
    if (
        not interpretation.has(ConversationIntent.PRICE_QUESTION)
        and not any(term in normalized for term in technical_terms)
    ):
        return None
    try:
        port = _require_booking_port(booking_port)
        catalog = tuple(await port.list_equipment_catalog(inbound.business_id))
    except (BookingPortUnavailable, BookingRequiresHandoff):
        return None
    if not catalog:
        return "No momento não há equipamentos ativos cadastrados para eu comparar."

    candidates = list(catalog)
    canonical_input = re.sub(r"[^a-z0-9]+", " ", normalized)
    mentioned_candidates = [
        item
        for item in candidates
        if (
            re.sub(
                r"[^a-z0-9]+",
                " ",
                normalize_portuguese(item.brand),
            ).strip()
            in canonical_input
            or (
                len(
                    re.sub(
                        r"[^a-z0-9]+",
                        " ",
                        normalize_portuguese(item.line),
                    ).strip()
                )
                >= 4
                and re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    normalize_portuguese(item.line),
                ).strip()
                in canonical_input
            )
        )
    ]
    if mentioned_candidates:
        candidates = mentioned_candidates

    requested_capacity = re.search(r"\b(\d{4,5})\s*btu\b", normalized)
    if requested_capacity:
        capacity = int(requested_capacity.group(1))
        candidates = [item for item in candidates if item.capacity_btu == capacity]

    cycle = _context_string(context, "equipment_cycle")
    if "quente frio" in normalized or "quente e frio" in normalized:
        cycle = "heat_cool"
    elif "so frio" in normalized or "somente frio" in normalized:
        cycle = "cold"
    if cycle == "heat_cool":
        candidates = [item for item in candidates if "heat_cool" in item.cycles]
    elif cycle == "cold":
        candidates = [
            item for item in candidates
            if "cold" in item.cycles and "heat_cool" not in item.cycles
        ]

    recommendation = context.get("recommended_equipment")
    technical_kind = (
        None
        if interpretation.has(ConversationIntent.PRICE_QUESTION)
        else _equipment_technical_question(normalized)
    )
    if technical_kind is not None:
        current_item = None
        current_item_id = (
            recommendation.get("item_id")
            if isinstance(recommendation, dict)
            else None
        )
        if isinstance(current_item_id, str):
            current_item = next(
                (item for item in catalog if item.item_id == current_item_id),
                None,
            )
        target_item: Any | None = current_item
        if target_item is None and len(candidates) == 1 and (
            mentioned_candidates or requested_capacity is not None
        ):
            target_item = candidates[0]
        if target_item is None and isinstance(recommendation, dict):
            target_item = recommendation
        if target_item is not None:
            technical_answer = _equipment_technical_answer(
                target_item,
                normalized,
            )
            if technical_answer is not None:
                return technical_answer
        if has_equipment_context and technical_kind in {
            "voltage",
            "dimensions",
            "sku",
            "cycle",
            "feature",
            "warranty",
            "consumption",
            "refrigerant",
            "noise",
        }:
            return (
                "Para responder esse detalhe sem inventar informação, preciso primeiro "
                "ter o modelo exato definido no catálogo. Vou manter o atendimento no "
                "ponto atual e seguimos com essa definição."
            )
    required_btu = (
        recommendation.get("required_btu_reference")
        if isinstance(recommendation, dict)
        else None
    )
    if not isinstance(required_btu, int) or isinstance(required_btu, bool):
        area = context.get("room_area_m2")
        people = context.get("room_people_max")
        if (
            isinstance(area, (int, float))
            and not isinstance(area, bool)
            and isinstance(people, int)
            and not isinstance(people, bool)
        ):
            try:
                required_btu = required_capacity_btu(float(area), people)
            except ValueError:
                required_btu = None
    if (
        not requested_capacity
        and isinstance(required_btu, int)
        and not isinstance(required_btu, bool)
    ):
        minimum = required_btu * 0.90
        candidates = [item for item in candidates if item.capacity_btu >= minimum]

    feature_aliases = {
        "alexa": ("alexa", "amazon alexa"),
        "wifi": ("wifi", "wi fi", "wi-fi"),
        "bluetooth": ("bluetooth",),
        "inverter": ("inverter",),
    }
    requested_features = [
        key
        for key, aliases in feature_aliases.items()
        if any(
            re.sub(r"[^a-z0-9]+", " ", alias) in canonical_input
            for alias in aliases
        )
    ]
    for feature in requested_features:
        aliases = tuple(
            re.sub(r"[^a-z0-9]+", " ", alias)
            for alias in feature_aliases[feature]
        )
        candidates = [
            item
            for item in candidates
            if any(
                any(
                    alias in re.sub(
                        r"[^a-z0-9]+",
                        " ",
                        normalize_portuguese(value),
                    )
                    for alias in aliases
                )
                for value in item.features
            )
            or (
                feature == "inverter"
                and "inverter" in normalize_portuguese(item.line)
            )
        ]

    stated_budget = (
        interpretation.equipment_budget_max
        or interpretation.total_budget_max
    )
    if (
        interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
        and not requested_capacity
        and not isinstance(required_btu, int)
        and stated_budget is not None
    ):
        constraints: list[str] = []
        if requested_features:
            constraints.append(", ".join(requested_features))
        constraints.append(
            f"o limite de {_format_brl(Decimal(str(stated_budget)))}"
        )
        return (
            "Certo. Vou considerar "
            + " e ".join(constraints)
            + " na recomendação. Primeiro preciso confirmar o perfil do ambiente "
            "para não indicar uma capacidade inadequada."
        )

    explicit_catalog_lookup = bool(mentioned_candidates) or requested_capacity is not None
    has_sizing_basis = (
        isinstance(required_btu, int)
        and not isinstance(required_btu, bool)
    )
    if not explicit_catalog_lookup and not has_sizing_basis:
        if requested_features:
            feature_label = ", ".join(requested_features)
            if not candidates:
                return (
                    f"Não tenho compatibilidade com {feature_label} explicitamente "
                    "cadastrada em uma opção do catálogo. Prefiro não assumir essa "
                    "função sem confirmação."
                )
            return (
                f"Encontrei opções com {feature_label} cadastradas, mas antes de "
                "indicar um modelo preciso confirmar o perfil do ambiente e a "
                "capacidade necessária em BTU."
            )
        if interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE):
            # Generic purchase/quote requests must enter the commercial flow
            # first. Do not let the catalog sorter manufacture a "compatible"
            # recommendation before the environment has been dimensioned.
            return None
        return (
            "Antes de comparar modelos, preciso confirmar o perfil do ambiente "
            "e a capacidade necessária em BTU para não indicar um aparelho inadequado."
        )

    explicit_equipment_budget = (
        interpretation.equipment_budget_max
        or (
            float(context["equipment_budget_max"])
            if isinstance(context.get("equipment_budget_max"), (int, float))
            and not isinstance(context.get("equipment_budget_max"), bool)
            else None
        )
    )
    total_budget = (
        interpretation.total_budget_max
        or (
            float(context["total_budget_max"])
            if isinstance(context.get("total_budget_max"), (int, float))
            and not isinstance(context.get("total_budget_max"), bool)
            else None
        )
    )
    budget = explicit_equipment_budget
    total_service_amount: Decimal | None = None
    if budget is None and total_budget is not None:
        if context.get("purchase_mode") == "both":
            service_id = _context_service_id(context)
            if service_id is not None:
                try:
                    service_plan = await port.estimate(
                        inbound.business_id,
                        service_id,
                        _requirements_from_context(context),
                    )
                except BookingRequiresHandoff:
                    service_plan = None
                if (
                    service_plan is not None
                    and service_plan.service.estimated_price is not None
                ):
                    total_service_amount = service_plan.service.estimated_price
                    budget = max(
                        0.0,
                        float(total_budget) - float(total_service_amount),
                    )
        else:
            budget = float(total_budget)

    current_price = (
        recommendation.get("price")
        if isinstance(recommendation, dict)
        else None
    )
    if (
        budget is None
        and ("mais barato" in normalized or "mais em conta" in normalized)
        and isinstance(current_price, (int, float))
        and not isinstance(current_price, bool)
    ):
        budget = float(current_price) - 0.01

    compatible = list(candidates)
    if budget is not None:
        candidates = [
            item
            for item in candidates
            if item.price is not None and item.price <= float(budget)
        ]

    if not candidates:
        if requested_features:
            feature_label = ", ".join(requested_features)
            if compatible and budget is not None:
                priced = [item for item in compatible if item.price is not None]
                if priced:
                    nearest = min(priced, key=lambda item: item.price or float("inf"))
                    context["equipment_suggestion"] = _equipment_suggestion_snapshot(
                        nearest,
                        required_btu=(
                            required_btu if isinstance(required_btu, int) else None
                        ),
                        selected_cycle=cycle,
                    )
                    return (
                        f"Não encontrei uma opção com {feature_label} dentro de "
                        f"{_format_brl(Decimal(str(budget)))}. "
                        f"A alternativa compatível mais próxima cadastrada é "
                        f"{nearest.brand} {nearest.line} {nearest.capacity_btu:,} BTU"
                        .replace(",", ".")
                        + f", por {_format_brl(Decimal(str(nearest.price)))}."
                    )
            return (
                f"Não tenho compatibilidade com {feature_label} explicitamente "
                "cadastrada em uma opção que atenda aos demais requisitos. "
                "Prefiro não assumir essa função sem confirmação no catálogo."
            )
        if budget is not None and compatible:
            priced = [item for item in compatible if item.price is not None]
            if priced:
                nearest = min(priced, key=lambda item: item.price or float("inf"))
                context["equipment_suggestion"] = _equipment_suggestion_snapshot(
                    nearest,
                    required_btu=(
                        required_btu if isinstance(required_btu, int) else None
                    ),
                    selected_cycle=cycle,
                )
                return (
                    f"Não encontrei uma opção compatível dentro de "
                    f"{_format_brl(Decimal(str(budget)))}. "
                    f"A alternativa mais próxima cadastrada é {nearest.brand} "
                    f"{nearest.line} {nearest.capacity_btu:,} BTU"
                    .replace(",", ".")
                    + f", por {_format_brl(Decimal(str(nearest.price)))}."
                )
        return "Não encontrei no catálogo ativo uma opção compatível com esses critérios."

    priced_candidates = [item for item in candidates if item.price is not None]
    selected = min(
        priced_candidates or candidates,
        key=lambda item: (
            item.price if item.price is not None else float("inf"),
            item.capacity_btu,
            item.brand,
            item.line,
        ),
    )
    cycle_label = (
        "quente/frio"
        if "heat_cool" in selected.cycles
        else "só frio"
    )
    price_text = (
        _format_brl(Decimal(str(selected.price)))
        if selected.price is not None
        else "valor a confirmar"
    )
    feature_text = ""
    if requested_features:
        feature_text = " com " + ", ".join(requested_features)
    context["equipment_suggestion"] = _equipment_suggestion_snapshot(
        selected,
        required_btu=(required_btu if isinstance(required_btu, int) else None),
        selected_cycle=cycle,
    )
    if not has_sizing_basis:
        return (
            f"No catálogo, {selected.brand} {selected.line} "
            f"{selected.capacity_btu:,} BTU".replace(",", ".")
            + f", {cycle_label}{feature_text}, está por {price_text}. "
            "Isso é uma consulta de catálogo, não uma indicação de capacidade "
            "para o ambiente."
        )
    return (
        f"Uma opção compatível é {selected.brand} {selected.line} "
        f"{selected.capacity_btu:,} BTU".replace(",", ".")
        + f", {cycle_label}{feature_text}, por {price_text}."
    )


async def _service_question_answer(
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
    booking_port: BookingAvailabilityPort | None,
) -> str | None:
    if not _has_service_question(interpretation):
        return None
    if interpretation.has(ConversationIntent.CANCEL_QUESTION):
        return (
            "Você pode cancelar um agendamento futuro por aqui quando quiser. "
            "Seu atendimento atual continua como está."
        )
    if interpretation.has(ConversationIntent.RESCHEDULE_QUESTION):
        return (
            "Você pode remarcar um agendamento futuro por aqui. "
            "Seu atendimento atual continua como está."
        )
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
        target_kind = _service_kind(services, target_id)
        service_budget = (
            interpretation.service_budget_max
            or (
                float(context["service_budget_max"])
                if isinstance(context.get("service_budget_max"), (int, float))
                and not isinstance(context.get("service_budget_max"), bool)
                else None
            )
        )
        if (
            service_budget is None
            and context.get("purchase_mode") != "both"
            and interpretation.total_budget_max is not None
        ):
            service_budget = interpretation.total_budget_max

        if plan is None or plan.service.estimated_price is None:
            parts.append(
                f"O valor de {target.label} depende de uma avaliação da equipe."
            )
        elif target_kind == "diagnostics":
            parts.append(
                "O valor base do atendimento técnico é "
                f"{_format_brl(plan.service.estimated_price)}. "
                "O valor final é confirmado depois do diagnóstico, porque pode haver "
                "necessidade de peças, materiais ou um reparo mais complexo."
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

        if (
            service_budget is not None
            and plan is not None
            and plan.service.estimated_price is not None
        ):
            budget_text = _format_brl(Decimal(str(service_budget)))
            if plan.service.estimated_price <= Decimal(str(service_budget)):
                parts.append(
                    f"Esse valor está dentro do limite de {budget_text} que você informou."
                )
            else:
                parts.append(
                    f"Esse valor fica acima do limite de {budget_text} que você informou."
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

        normalized = normalize_portuguese(inbound.body or "")
        target_kind = _service_kind(services, target_id)
        evidence_question = any(
            term in normalized
            for term in ("foto", "video", "imagem", "gravar")
        )
        tubing_question = any(
            term in normalized
            for term in ("tubulacao", "metragem", "quantos metros", "metro incluso")
        )
        warranty_question = "garantia" in normalized

        if target_kind == "diagnostics" and evidence_question:
            evidence_parts = [
                "Na manutenção/diagnóstico, as evidências ajudam o técnico antes do atendimento."
            ]
            if context.get("equipment_photo_received") is True:
                evidence_parts.append("A foto do aparelho já ficou registrada.")
            elif _context_string(context, "equipment_model") is None:
                evidence_parts.append(
                    "Se você não souber a marca/modelo, pode enviar uma foto do aparelho."
                )
            if context.get("issue_video_received") is True:
                evidence_parts.append("O vídeo do funcionamento também já ficou registrado.")
            elif context.get("issue_video_required") is True:
                evidence_parts.append(
                    "Como foi relatado barulho/ruído, também é útil enviar um vídeo curto dele funcionando."
                )
            parts.append(" ".join(evidence_parts))
        elif tubing_question:
            included = getattr(details, "included_tubing_meters", None)
            extra_price = getattr(details, "extra_tubing_price", None)
            if isinstance(included, Decimal):
                answer = f"O serviço cadastrado inclui {included:g} m de tubulação."
                if isinstance(extra_price, Decimal):
                    answer += (
                        " Acima disso, o metro adicional cadastrado é "
                        f"{_format_brl(extra_price)}."
                    )
                else:
                    answer += (
                        " O valor de metragem adicional precisa ser confirmado "
                        "conforme a configuração do serviço."
                    )
                parts.append(answer)
            else:
                parts.append(
                    "A metragem de tubulação incluída não está cadastrada com segurança "
                    "para esse serviço. Prefiro não assumir um valor."
                )
        elif warranty_question:
            description = getattr(details, "description", None)
            if (
                isinstance(description, str)
                and "garantia" in normalize_portuguese(description)
            ):
                parts.append(description.strip())
            else:
                parts.append(
                    "A garantia específica desse serviço não está detalhada no cadastro "
                    "que tenho aqui. Prefiro não inventar prazo ou cobertura; a equipe "
                    "pode confirmar esse ponto."
                )
        else:
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


def _message_can_answer_pending_state(
    state: ConversationState,
    inbound: ConversationInput,
    context: dict[str, Any],
    interpretation: Interpretation,
) -> bool:
    """Return whether a free-text message can safely fill the active slot."""

    if inbound.interactive_id is not None:
        return True
    body = (inbound.body or "").strip()
    normalized = normalize_portuguese(body)
    if not normalized:
        return False
    if state is ConversationState.CUSTOMER_NAME:
        return extract_customer_name(body, allow_bare=True) is not None
    if state is ConversationState.BOOKING_SERVICE:
        return interpretation.has(ConversationIntent.SERVICE_INTENT)
    if state is ConversationState.BOOKING_QUANTITY:
        return parse_number_answer(body) is not None
    if state is ConversationState.BOOKING_ACCESS:
        return any(
            phrase in normalized
            for phrase in (
                "normal",
                "facil",
                "sem dificuldade",
                "dificil",
                "complicado",
                "escada",
                "nao sei",
                "nao tenho certeza",
            )
        )
    if state is ConversationState.BOOKING_ADDRESS:
        return _looks_like_address(body)
    if state is ConversationState.BOOKING_EQUIPMENT_OWNERSHIP:
        return any(
            phrase in normalized
            for phrase in (
                "ja tenho",
                "tenho o aparelho",
                "so instalacao",
                "somente instalacao",
                "apenas instalar",
                "quero cotar",
                "quero comprar",
                "preciso comprar",
                "nao tenho aparelho",
                "nao tenho o ar",
            )
        )
    if state is ConversationState.BOOKING_EQUIPMENT_MODEL:
        if normalized in {
            "nao",
            "nao tenho",
            "nao sei",
            "nao sei o modelo",
            "nao conheco",
            "sem preferencia",
            "pode recomendar",
        }:
            return True
        return bool(
            context.get("equipment_model")
            or re.search(r"\b\d{4,5}\s*btu\b", normalized)
            or (2 <= len(body) <= 180 and "?" not in body)
        )
    if state is ConversationState.BOOKING_EQUIPMENT_PROFILE:
        return _has_profile_fact(enrich_context_from_message({}, body))
    if state is ConversationState.BOOKING_EQUIPMENT_DELIVERY:
        return any(
            phrase in normalized
            for phrase in (
                "retirar",
                "buscar",
                "retirada",
                "receber",
                "entregar",
                "entrega",
                "com a instalacao",
                "no dia da instalacao",
                "mesmo endereco",
                "outro endereco",
            )
        )
    if state is ConversationState.BOOKING_INSTALLATION_HEIGHT:
        return bool(
            any(
                phrase in normalized
                for phrase in (
                    "mais de 3",
                    "acima de 3",
                    "passa de 3",
                    "ate 3",
                    "menos de 3",
                    "abaixo de 3",
                    "nao passa de 3",
                )
            )
            or _decimal_from_text(body) is not None
        )
    if state is ConversationState.BOOKING_PROPERTY:
        focused = correction_focus(body)
        return any(
            token in focused
            for token in (
                "casa",
                "residencia",
                "predio",
                "edificio",
                "apartamento",
                "apto",
                "condominio",
            )
        )
    if state is ConversationState.BOOKING_BUILDING_HOURS:
        return _time_window_from_text(body) is not None
    if state is ConversationState.BOOKING_GATE_DETAILS:
        return (
            2 <= len(body) <= 300
            and "?" not in body
            and not any(
                normalized.startswith(prefix)
                for prefix in (
                    "como ",
                    "qual ",
                    "quanto ",
                    "quando ",
                    "onde ",
                    "por que ",
                    "porque ",
                )
            )
        )
    if state is ConversationState.BOOKING_TUBING:
        return _tubing_unknown_text(normalized) or _decimal_from_text(body) is not None
    if state is ConversationState.BOOKING_SITE_LIMIT:
        return (
            normalized in {"sem limite", "nao tem limite", "nenhum limite"}
            or _site_limit(None, body) is not False
        )
    if state in {
        ConversationState.BOOKING_DATE,
        ConversationState.BOOKING_WEEKDAY,
    }:
        return bool(
            weekday_from_text(body) is not None
            or re.search(
                r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
                body,
            )
        )
    if state is ConversationState.BOOKING_TIME:
        return bool(
            re.search(
                r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d|h)\b",
                normalized,
            )
        )
    if state is ConversationState.BOOKING_ATTENDEE:
        return any(
            phrase in normalized
            for phrase in (
                "sim",
                "sou eu",
                "eu vou estar",
                "eu estarei",
                "eu mesmo",
                "eu mesma",
                "outra pessoa",
                "nao",
                "nao vou estar",
            )
        )
    if state is ConversationState.BOOKING_ATTENDEE_NAME:
        return extract_customer_name(body, allow_bare=True) is not None
    if state is ConversationState.BOOKING_PHONE_CONFIRM:
        return (
            normalized in {"sim", "pode", "correto", "isso", "outro", "outro numero", "nao"}
            or _phone_from_text(body) is not None
        )
    if state is ConversationState.BOOKING_CONFIRM:
        return normalized in {
            "confirmar",
            "confirmo",
            "sim",
            "pode confirmar",
            "pode",
            "voltar",
            "outro horario",
            "trocar horario",
            "cancelar",
            "cancela",
            "nao",
        }
    if state is ConversationState.QUOTE_DECISION:
        return any(
            phrase in normalized
            for phrase in (
                "consultar agenda",
                "agendar",
                "quero marcar",
                "ver horario",
                "so cotacao",
                "so queria cotacao",
                "obrigado",
                "era isso",
            )
        )
    return False

def _purchase_mode_action_from_text(
    normalized: str,
    interpretation: Interpretation | None = None,
) -> str | None:
    if normalized in {
        "comprar",
        "comprar aparelho",
        "so comprar",
        "somente comprar",
        "compra",
    }:
        return EQUIPMENT_PURCHASE
    if normalized in {
        "os dois",
        "ambos",
        "compra e instalacao",
        "comprar e instalar",
    }:
        return EQUIPMENT_BOTH
    if "instal" in normalized:
        return EQUIPMENT_INSTALLATION
    if interpretation is not None and interpretation.has(
        ConversationIntent.EQUIPMENT_PURCHASE
    ):
        return EQUIPMENT_PURCHASE
    return None


def _purchase_mode_for_action(action: str | None) -> str | None:
    return {
        EQUIPMENT_INSTALLATION: "installation",
        EQUIPMENT_PURCHASE: "purchase",
        EQUIPMENT_BOTH: "both",
    }.get(action)


def _request_change_confirmation(
    state: ConversationState,
    context: dict[str, Any],
    action: str,
    label: str,
) -> ConversationTransition:
    return _transition(
        state,
        {
            **context,
            "pending_change_action": action,
            "pending_change_label": label,
        },
        change_confirmation_message(label),
    )


async def _resume_pending_question(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
    *,
    prefix: str,
) -> ConversationTransition:
    state = _canonical_state(conversation.state)
    customer_name = conversation.customer_name
    simple_prompts: dict[ConversationState, OutboundMessage] = {
        ConversationState.CUSTOMER_NAME: name_request_message(
            "Antes de continuar, como você gostaria de ser chamado?"
        ),
        ConversationState.BOOKING_QUANTITY: quantity_selection_message(),
        ConversationState.BOOKING_ACCESS: access_selection_message(),
        ConversationState.BOOKING_ADDRESS: address_request_message(
            "Qual é o endereço de entrega, com rua, número e cidade?"
            if context.get("address_purpose") == "delivery"
            else "Qual é o endereço, com rua, número e cidade?"
        ),
        ConversationState.BOOKING_EQUIPMENT_OWNERSHIP: installation_equipment_status_message(),
        ConversationState.BOOKING_EQUIPMENT_MODEL: equipment_model_request_message(),
        ConversationState.BOOKING_EQUIPMENT_PROFILE: _equipment_profile_prompt(
            missing_equipment_profile_fields(context)
        ),
        ConversationState.BOOKING_EQUIPMENT_DELIVERY: (
            delivery_installation_address_message()
            if context.get("delivery_installation_match_pending") is True
            else equipment_delivery_message(
                include_with_installation=context.get("purchase_mode") == "both"
            )
        ),
        ConversationState.BOOKING_INSTALLATION_HEIGHT: installation_height_message(),
        ConversationState.BOOKING_PROPERTY: property_type_message(),
        ConversationState.BOOKING_BUILDING_HOURS: building_hours_message(),
        ConversationState.BOOKING_GATE_DETAILS: gate_details_message(),
        ConversationState.BOOKING_TUBING: tubing_length_message(),
        ConversationState.BOOKING_SITE_LIMIT: site_limit_message(),
        ConversationState.BOOKING_ATTENDEE: attendee_message(customer_name),
        ConversationState.BOOKING_ATTENDEE_NAME: attendee_name_message(),
        ConversationState.BOOKING_PHONE_CONFIRM: phone_confirmation_message(
            _context_string(context, "whatsapp_contact_phone") or "este número"
        ),
        ConversationState.BOOKING_CONFIRM: booking_confirmation_message(
            "Confira os dados e confirme quando estiver tudo certo."
        ),
        ConversationState.QUOTE_DECISION: quote_decision_message(
            "Posso consultar a agenda para esse atendimento?"
        ),
    }
    prompt = simple_prompts.get(state)
    if prompt is not None:
        return _prepend_transition_body(_transition(state, context, prompt), prefix)

    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(state, context, booking_unavailable_message())
    if state is ConversationState.BOOKING_SERVICE:
        services = _snapshot_options(await port.list_services(inbound.business_id))
        transition = _transition(
            state,
            context,
            service_selection_message(services),
        )
    elif state in {ConversationState.BOOKING_DATE, ConversationState.BOOKING_WEEKDAY}:
        service_id = _context_service_id(context)
        if service_id is None:
            transition = await _restart_service_selection(inbound, port)
        else:
            dates = _snapshot_options(
                await port.list_dates(
                    inbound.business_id,
                    service_id,
                    _requirements_from_context(context),
                )
            )
            transition = _transition(
                ConversationState.BOOKING_DATE,
                context,
                date_selection_message(dates),
            )
    elif state is ConversationState.BOOKING_TIME:
        service_id = _context_service_id(context)
        selected_date = _context_string(context, "selected_date")
        if service_id is None or selected_date is None:
            transition = await _restart_service_selection(inbound, port)
        else:
            times = _snapshot_options(
                await port.list_times(
                    inbound.business_id,
                    service_id,
                    selected_date,
                    _requirements_from_context(context),
                )
            )
            transition = _transition(
                state,
                context,
                time_selection_message(
                    times,
                    body=_time_prompt(selected_date, times, customer_name),
                ),
            )
    else:
        transition = _transition(state, context, _text_message("Podemos continuar daqui."))
    return _prepend_transition_body(transition, prefix)


async def _handle_pending_change(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    cleaned = dict(context)
    pending_action = _context_string(cleaned, "pending_change_action")
    pending_service_id = _context_string(cleaned, "pending_service_change_id")
    for key in (
        "pending_change_action",
        "pending_change_label",
        "pending_service_change_id",
        "pending_service_change_label",
    ):
        cleaned.pop(key, None)

    if action == CHANGE_KEEP:
        return await _resume_pending_question(
            conversation,
            inbound,
            cleaned,
            booking_port,
            prefix="Certo, mantenho sua escolha.",
        )

    if pending_service_id is not None:
        return await _handle_service(
            inbound,
            context_for_service_change(cleaned),
            f"service:{pending_service_id}",
            booking_port,
            interpretation=DeterministicConversationInterpreter().interpret(inbound.body),
            customer_name=conversation.customer_name,
        )
    if pending_action is not None and pending_action.startswith("service:"):
        return await _handle_service(
            inbound,
            context_for_service_change(cleaned),
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action is not None and pending_action.startswith("date:"):
        return await _handle_date(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action is not None and pending_action.startswith("time:"):
        return await _handle_time(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action in {
        EQUIPMENT_INSTALLATION,
        EQUIPMENT_PURCHASE,
        EQUIPMENT_BOTH,
    }:
        return await _handle_service(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action in {
        EQUIPMENT_PREF_MODERN,
        EQUIPMENT_PREF_COST_BENEFIT,
        EQUIPMENT_PREF_ECONOMY,
        EQUIPMENT_CYCLE_COLD,
        EQUIPMENT_CYCLE_HEAT_COOL,
    }:
        return await _handle_equipment_profile(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action is not None and pending_action.startswith("quantity:"):
        return await _handle_quantity(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action in {ACCESS_NORMAL, ACCESS_DIFFICULT, ACCESS_UNKNOWN}:
        return await _handle_access(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action in {HEIGHT_AT_MOST_3M, HEIGHT_OVER_3M}:
        return await _handle_installation_height(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )
    if pending_action in {
        EQUIPMENT_DELIVERY_PICKUP,
        EQUIPMENT_DELIVERY_ADDRESS,
        EQUIPMENT_DELIVERY_WITH_INSTALLATION,
        EQUIPMENT_INSTALLATION_SAME_ADDRESS,
        EQUIPMENT_INSTALLATION_OTHER_ADDRESS,
    }:
        return await _handle_equipment_delivery(
            inbound,
            cleaned,
            pending_action,
            booking_port,
            customer_name=conversation.customer_name,
        )

    property_by_action = {
        PROPERTY_HOUSE: "house",
        PROPERTY_BUILDING: "building",
        PROPERTY_CONDOMINIUM: "condominium",
    }
    if pending_action in property_by_action:
        updated = invalidate_changed_facts(
            cleaned,
            {**cleaned, "property_type": property_by_action[pending_action]},
        )
        try:
            port = _require_booking_port(booking_port)
            _, intake = await _context_intake(inbound, updated, port)
        except (BookingPortUnavailable, BookingRequiresHandoff):
            return _transition(
                _canonical_state(conversation.state),
                updated,
                booking_unavailable_message(),
            )
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=conversation.customer_name,
        )
    return await _resume_pending_question(
        conversation,
        inbound,
        cleaned,
        booking_port,
        prefix="Certo, seguimos daqui.",
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
    if (
        state is ConversationState.BOOKING_SERVICE
        and action.startswith("service:")
    ):
        return None
    if state is ConversationState.BOOKING_DATE and action.startswith("date:"):
        return None
    if state is ConversationState.BOOKING_TIME and action.startswith("time:"):
        return None
    if state is ConversationState.BOOKING_QUANTITY and action.startswith("quantity:"):
        return None

    expected_actions: dict[ConversationState, set[str]] = {
        ConversationState.BOOKING_ACCESS: {
            ACCESS_NORMAL,
            ACCESS_DIFFICULT,
            ACCESS_UNKNOWN,
        },
        ConversationState.BOOKING_EQUIPMENT_OWNERSHIP: {
            EQUIPMENT_HAS,
            EQUIPMENT_NEEDS,
        },
        ConversationState.BOOKING_EQUIPMENT_MODEL: {
            EQUIPMENT_MODEL_KNOWN,
            EQUIPMENT_MODEL_RECOMMEND,
        },
        ConversationState.BOOKING_EQUIPMENT_PROFILE: {
            EQUIPMENT_PREF_MODERN,
            EQUIPMENT_PREF_COST_BENEFIT,
            EQUIPMENT_PREF_ECONOMY,
            EQUIPMENT_CYCLE_COLD,
            EQUIPMENT_CYCLE_HEAT_COOL,
            EQUIPMENT_SPACE_NO_LIMIT,
        },
        ConversationState.BOOKING_SERVICE: {
            EQUIPMENT_INSTALLATION,
            EQUIPMENT_PURCHASE,
            EQUIPMENT_BOTH,
        },
        ConversationState.BOOKING_EQUIPMENT_DELIVERY: {
            EQUIPMENT_DELIVERY_PICKUP,
            EQUIPMENT_DELIVERY_ADDRESS,
            EQUIPMENT_DELIVERY_WITH_INSTALLATION,
            EQUIPMENT_INSTALLATION_SAME_ADDRESS,
            EQUIPMENT_INSTALLATION_OTHER_ADDRESS,
        },
        ConversationState.BOOKING_INSTALLATION_HEIGHT: {
            HEIGHT_AT_MOST_3M,
            HEIGHT_OVER_3M,
        },
        ConversationState.BOOKING_PROPERTY: {
            PROPERTY_HOUSE,
            PROPERTY_BUILDING,
            PROPERTY_CONDOMINIUM,
        },
        ConversationState.BOOKING_ATTENDEE: {
            ATTENDEE_CUSTOMER,
            ATTENDEE_OTHER,
        },
        ConversationState.BOOKING_PHONE_CONFIRM: {
            PHONE_CONFIRM,
            PHONE_OTHER,
        },
        ConversationState.QUOTE_DECISION: {
            QUOTE_SCHEDULE,
            QUOTE_FINISH,
        },
        ConversationState.POST_BOOKING_HELP: {
            POST_BOOKING_HELP_YES,
            POST_BOOKING_HELP_NO,
        },
    }
    if action in expected_actions.get(state, set()):
        return None

    if action.startswith("quantity:"):
        quantity = _quantity(action, None)
        if quantity is not None:
            return _request_change_confirmation(
                state, context, action, f"{quantity} aparelho(s)"
            )

    change_labels = {
        ACCESS_NORMAL: "acesso normal",
        ACCESS_DIFFICULT: "acesso difícil",
        ACCESS_UNKNOWN: "condição de acesso ainda não confirmada",
        HEIGHT_AT_MOST_3M: "instalação em até 3 metros",
        HEIGHT_OVER_3M: "instalação acima de 3 metros",
        EQUIPMENT_INSTALLATION: "somente instalação",
        EQUIPMENT_PURCHASE: "comprar o aparelho",
        EQUIPMENT_BOTH: "compra + instalação",
        EQUIPMENT_PREF_MODERN: "um modelo mais moderno",
        EQUIPMENT_PREF_COST_BENEFIT: "custo-benefício",
        EQUIPMENT_PREF_ECONOMY: "maior economia",
        EQUIPMENT_CYCLE_COLD: "só frio",
        EQUIPMENT_CYCLE_HEAT_COOL: "quente/frio",
        EQUIPMENT_DELIVERY_PICKUP: "retirar o aparelho",
        EQUIPMENT_DELIVERY_ADDRESS: "receber o aparelho no endereço",
        EQUIPMENT_DELIVERY_WITH_INSTALLATION: "levar o aparelho junto com a instalação",
        PROPERTY_HOUSE: "casa",
        PROPERTY_BUILDING: "prédio/apartamento",
        PROPERTY_CONDOMINIUM: "condomínio",
    }
    label = change_labels.get(action)
    if label is None and action.startswith("service:"):
        label = "outro serviço"
    elif label is None and action.startswith("date:"):
        label = f"a data {action.removeprefix('date:')}"
    elif label is None and action.startswith("time:"):
        label = f"o horário {action.removeprefix('time:')}"
    if label is not None:
        return _request_change_confirmation(state, context, action, label)

    if state is ConversationState.CUSTOMER_NAME:
        return _transition(
            state,
            context,
            name_request_message(
                "Sem problema. Antes de continuar, qual é o seu nome?"
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
                    "Pode me enviar o endereço com rua, número e cidade?"
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
    if state is ConversationState.BOOKING_EQUIPMENT_OWNERSHIP:
        return _transition(state, context, installation_equipment_status_message())
    if state is ConversationState.BOOKING_EQUIPMENT_MODEL:
        return _transition(state, context, equipment_model_known_message())
    if state is ConversationState.BOOKING_EQUIPMENT_PROFILE:
        return _transition(
            state,
            context,
            equipment_profile_message(missing_equipment_profile_fields(context)),
        )
    if state is ConversationState.BOOKING_INSTALLATION_HEIGHT:
        return _transition(state, context, installation_height_message())
    if state is ConversationState.BOOKING_PROPERTY:
        return _transition(state, context, property_type_message())
    if state is ConversationState.BOOKING_BUILDING_HOURS:
        return _transition(state, context, building_hours_message())
    if state is ConversationState.BOOKING_GATE_DETAILS:
        return _transition(state, context, gate_details_message())
    if state is ConversationState.BOOKING_ATTENDEE:
        return _transition(state, context, attendee_message(customer_name))
    if state is ConversationState.BOOKING_ATTENDEE_NAME:
        return _transition(state, context, attendee_name_message())
    if state is ConversationState.BOOKING_PHONE_CONFIRM:
        phone = _context_string(context, "whatsapp_contact_phone") or "este número"
        return _transition(state, context, phone_confirmation_message(phone))
    if state is ConversationState.QUOTE_DECISION:
        return _transition(
            state,
            context,
            quote_decision_message(
                "Posso consultar a agenda para esse atendimento?"
            ),
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
        attempts[slot] = next_attempt
        return _transition(
            ConversationState.HUMAN_HANDOFF,
            {**context, "repair_attempts": attempts},
            _text_message(handoff_body),
            automation_enabled=False,
            handoff_status="waiting",
        )
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

_CITY_ABBREVIATIONS = {
    "sjc": ("São José dos Campos", "SP"),
    "bh": ("Belo Horizonte", "MG"),
    "bsb": ("Brasília", "DF"),
    "poa": ("Porto Alegre", "RS"),
    "cwb": ("Curitiba", "PR"),
    "fln": ("Florianópolis", "SC"),
    "ssa": ("Salvador", "BA"),
    "rec": ("Recife", "PE"),
    "for": ("Fortaleza", "CE"),
    "gyn": ("Goiânia", "GO"),
    "vix": ("Vitória", "ES"),
    "nat": ("Natal", "RN"),
    "jpa": ("João Pessoa", "PB"),
    "slz": ("São Luís", "MA"),
    "the": ("Teresina", "PI"),
    "mcz": ("Maceió", "AL"),
    "aju": ("Aracaju", "SE"),
    "bel": ("Belém", "PA"),
    "mao": ("Manaus", "AM"),
    "pvh": ("Porto Velho", "RO"),
    "rbr": ("Rio Branco", "AC"),
    "mcp": ("Macapá", "AP"),
    "bvb": ("Boa Vista", "RR"),
    "pmw": ("Palmas", "TO"),
    "cgr": ("Campo Grande", "MS"),
    "cgb": ("Cuiabá", "MT"),
}
_NEIGHBORHOOD_MARKERS = {
    "bairro", "jardim", "jd", "parque", "pq", "vila", "vl",
    "residencial", "loteamento", "conjunto", "centro",
}


def _city_alias_from_text(value: str) -> tuple[str, str] | None:
    segments = [
        normalize_portuguese(segment)
        for segment in re.split(r"[,\n]+", value)
        if normalize_portuguese(segment)
    ]
    candidates = segments[-2:] if segments else [normalize_portuguese(value)]
    for segment in reversed(candidates):
        tokens = [
            token
            for token in segment.split()
            if token not in _BRAZIL_STATE_CODES
        ]
        if len(tokens) == 1 and tokens[0] in _CITY_ABBREVIATIONS:
            return _CITY_ABBREVIATIONS[tokens[0]]
    normalized = normalize_portuguese(value)
    if normalized in _CITY_ABBREVIATIONS:
        return _CITY_ABBREVIATIONS[normalized]
    return None


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
    updated["service_id"] = str(service_id)
    if context.get("address_purpose") == "delivery":
        updated["delivery_address"] = address.to_snapshot()
        updated["delivery_installation_match_pending"] = (
            context.get("purchase_only") is not True
        )
    else:
        updated["service_address"] = address.to_snapshot()
    updated.pop("address_purpose", None)
    updated.pop("pending_service_address", None)
    updated.pop("pending_address_city_guess", None)
    updated.pop("awaiting_address_city", None)
    return invalidate_changed_facts(context, updated)


def _installation_service_option(
    services: Sequence[BookingOption],
) -> BookingOption | None:
    ranked = [
        item
        for item in services
        if "instal" in normalize_portuguese(item.label)
        and any(
            token in normalize_portuguese(item.label)
            for token in ("ar condicionado", "split")
        )
    ]
    if ranked:
        return ranked[0]
    return next(
        (
            item
            for item in services
            if "instal" in normalize_portuguese(item.label)
        ),
        None,
    )


def _service_kind(
    services: Sequence[BookingOption],
    service_id: uuid.UUID,
) -> str:
    selected = next(
        (item for item in services if item.id == str(service_id)),
        None,
    )
    normalized = normalize_portuguese(selected.label if selected else "")
    if "instal" in normalized:
        return "installation"
    if any(token in normalized for token in ("limpeza", "higien", "lavagem")):
        return "cleaning"
    if any(token in normalized for token in ("recarga", "gas", "vazamento")):
        return "gas_recharge"
    if any(token in normalized for token in ("diagnost", "corretiv")):
        return "diagnostics"
    if "preventiv" in normalized or "revis" in normalized:
        return "preventive"
    return "other"


async def _safe_service_details(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    service_id: uuid.UUID,
):
    try:
        return await port.get_service_details(
            inbound.business_id,
            service_id,
        )
    except BookingRequiresHandoff:
        return None


async def _with_equipment_recommendation(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
) -> dict[str, Any]:
    area = context.get("room_area_m2")
    people = context.get("room_people_max")
    preference = context.get("equipment_preference")
    climate_mode = context.get("equipment_cycle")
    if (
        not isinstance(area, (int, float))
        or isinstance(area, bool)
        or not isinstance(people, int)
        or isinstance(people, bool)
        or preference not in {"modern", "cost_benefit", "economy"}
        or climate_mode not in {"cold", "heat_cool"}
    ):
        return dict(context)

    indoor_space = _space_tuple(context, "indoor")
    outdoor_space = _space_tuple(context, "outdoor")
    catalog = await port.list_equipment_catalog(inbound.business_id)

    budget_max = context.get("equipment_budget_max")
    if not isinstance(budget_max, (int, float)) or isinstance(budget_max, bool):
        budget_max = None
    total_budget = context.get("total_budget_max")
    if (
        budget_max is None
        and context.get("purchase_mode") == "both"
        and isinstance(total_budget, (int, float))
        and not isinstance(total_budget, bool)
    ):
        service_id = _context_service_id(context)
        if service_id is not None:
            try:
                service_plan = await port.estimate(
                    inbound.business_id,
                    service_id,
                    _requirements_from_context(context),
                )
            except BookingRequiresHandoff:
                service_plan = None
            if (
                service_plan is not None
                and service_plan.service.estimated_price is not None
            ):
                budget_max = max(
                    0.0,
                    float(total_budget)
                    - float(service_plan.service.estimated_price),
                )

    recommendation = recommend_equipment(
        float(area),
        people,
        preference,
        climate_mode=climate_mode,
        indoor_space=indoor_space,
        outdoor_space=outdoor_space,
        entries=catalog,
        budget_max=float(budget_max) if budget_max is not None else None,
    )
    return {
        **context,
        "recommended_equipment": {
            "item_id": recommendation.item_id,
            "label": recommendation.label,
            "brand": recommendation.brand,
            "line": recommendation.line,
            "capacity_btu": recommendation.capacity_btu,
            "preference": recommendation.segment,
            "cycles": list(recommendation.cycles),
            "selected_cycle": recommendation.selected_cycle,
            "features": list(recommendation.features),
            "source_url": recommendation.source_url,
            "image_url": recommendation.image_url,
            "condenser_form": recommendation.condenser_form,
            "price": recommendation.price,
            "required_btu_reference": recommendation.required_btu,
            "within_budget": recommendation.within_budget,
            "budget_max": budget_max,
        },
    }


def _space_tuple(
    context: dict[str, Any],
    target: str,
) -> tuple[float, float, float | None] | None:
    if context.get(f"{target}_space_unrestricted") is True:
        return None
    width = context.get(f"{target}_space_width_cm")
    height = context.get(f"{target}_space_height_cm")
    depth = context.get(f"{target}_space_depth_cm")
    if (
        isinstance(width, (int, float))
        and not isinstance(width, bool)
        and isinstance(height, (int, float))
        and not isinstance(height, bool)
    ):
        return (
            float(width),
            float(height),
            float(depth) if isinstance(depth, (int, float)) and not isinstance(depth, bool) else None,
        )
    return None

def _time_window_from_text(
    value: str | None,
) -> tuple[str, str] | None:
    raw = value or ""
    match = re.search(
        r"\b(?:das?\s*)?(\d{1,2})(?::(\d{2}))?\s*(?:h|hs|hr|hrs|hora|horas)?"
        r"\s*(?:às|as|até|ate|a|-|–|—)\s*(\d{1,2})(?::(\d{2}))?"
        r"\s*(?:h|hs|hr|hrs|hora|horas)?\b",
        raw.casefold(),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    start_hour, start_minute, end_hour, end_minute = match.groups()
    start_h, end_h = int(start_hour), int(end_hour)
    start_m, end_m = int(start_minute or 0), int(end_minute or 0)
    if not (
        0 <= start_h <= 23
        and 0 <= end_h <= 23
        and 0 <= start_m <= 59
        and 0 <= end_m <= 59
    ):
        return None
    start_value = f"{start_h:02d}:{start_m:02d}"
    end_value = f"{end_h:02d}:{end_m:02d}"
    return (start_value, end_value) if start_value < end_value else None


def _phone_from_text(value: str | None) -> str | None:
    raw = value or ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55") and 12 <= len(digits) <= 13:
        return f"+{digits}"
    if 10 <= len(digits) <= 11:
        return f"+55{digits}"
    return None


def _context_time(
    context: dict[str, Any],
    key: str,
) -> time | None:
    value = _context_string(context, key)
    if value is None:
        return None
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return None
    return parsed


def _quote_resume_requested(
    inbound: ConversationInput,
    interpretation: Interpretation,
    action: str | None,
) -> bool:
    if action in {QUOTE_SCHEDULE, MENU_BOOK}:
        return True
    normalized = normalize_portuguese(inbound.body or "")
    continuation_phrases = (
        "ok vamos seguir",
        "vamos seguir",
        "quero seguir",
        "podemos seguir",
        "pode seguir",
        "vamos continuar",
        "quero continuar",
        "pode continuar",
        "quero consultar a agenda",
        "consultar agenda",
        "ver agenda",
        "seguir com o agendamento",
    )
    return (
        any(phrase in normalized for phrase in continuation_phrases)
        or interpretation.has(ConversationIntent.BOOK)
        or interpretation.has(ConversationIntent.AVAILABILITY)
    )


async def _resume_paused_quote_if_requested(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    state: ConversationState,
    context: dict[str, Any],
    interpretation: Interpretation,
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition | None:
    if (
        state not in {ConversationState.COMPLETED, ConversationState.MENU}
        or context.get("quote_paused") is not True
        or _context_service_id(context) is None
    ):
        return None

    if _quote_resume_requested(inbound, interpretation, action):
        try:
            port = _require_booking_port(booking_port)
        except BookingPortUnavailable:
            return _transition(
                state,
                context,
                booking_unavailable_message(),
            )
        updated = {
            **context,
            "quote_presented": True,
        }
        updated.pop("quote_paused", None)
        updated.pop("request_mode", None)
        return await _offer_dates(
            inbound,
            port,
            updated,
            customer_name=conversation.customer_name,
        )

    if (
        action is None
        and isinstance(inbound.body, str)
        and inbound.body.strip()
        and (
            interpretation.intent is ConversationIntent.UNKNOWN
            or interpretation.has_act(ConversationAct.SOCIAL)
        )
    ):
        return _transition(
            ConversationState.QUOTE_DECISION,
            context,
            quote_decision_message(
                "Sua cotação continua registrada. Quer que eu consulte a agenda "
                "para seguir com a instalação?"
            ),
        )
    return None


def _friendly_fallback(configured: str, *, variant: int = 0) -> str:
    normalized = normalize_portuguese(configured)
    if normalized.startswith("nao entendi"):
        if variant % 2:
            return (
                "Claro. Posso continuar com instalação, limpeza, manutenção, "
                "recarga de gás ou agendamento. Me diga o que você quer fazer agora."
            )
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
    fallback_context = _clean_context(conversation.context)
    variant = fallback_context.get("fallback_variant")
    variant_index = variant if isinstance(variant, int) else 0
    fallback_context["fallback_variant"] = 1 if variant_index == 0 else 0
    return _transition(
        ConversationState.MENU,
        fallback_context,
        _text_message(
            _friendly_fallback(
                conversation.fallback_message,
                variant=variant_index,
            )
        ),
    )

async def _handle_menu(
    inbound: ConversationInput,
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    context: dict[str, Any] | None = None,
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
    active_context = _clean_context(context or {})
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
            context=active_context,
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
    purchase_action = action if action in {
        EQUIPMENT_PURCHASE,
        EQUIPMENT_BOTH,
        EQUIPMENT_INSTALLATION,
    } else None

    if clarification == "equipment_purchase" and purchase_action is None:
        purchase_action = _purchase_mode_action_from_text(
            normalized,
            interpretation,
        )
        if purchase_action is None:
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

    if (
        purchase_action is None
        and interpretation is not None
        and interpretation.has(ConversationIntent.EQUIPMENT_PURCHASE)
    ):
        updated = {
            **context,
            "service_clarification": "equipment_purchase",
            "request_mode": "quote",
        }
        return _transition(
            ConversationState.BOOKING_SERVICE,
            updated,
            equipment_purchase_clarification_message(),
        )

    if purchase_action is not None:
        purchase_mode = _purchase_mode_for_action(purchase_action)
        if purchase_mode is None:
            return _transition(
                ConversationState.BOOKING_SERVICE,
                context,
                equipment_purchase_clarification_message(retry=True),
            )
        installation = _installation_service_option(services)
        if installation is None:
            return _handoff_transition(
                "Entendi o que você procura, mas não encontrei uma instalação "
                "residencial configurada para montar a cotação. Vou chamar a equipe."
            )
        service_id = uuid.UUID(installation.id)
        updated_context = {
            **context,
            "service_id": str(service_id),
            "purchase_mode": purchase_mode,
        }
        updated_context.pop("service_clarification", None)
        if purchase_action == EQUIPMENT_INSTALLATION:
            updated_context["equipment_ownership"] = "has_equipment"
            updated_context["purchase_only"] = False
            updated_context.pop("request_mode", None)
            updated_context.pop("recommended_equipment", None)
            updated_context.pop("recommendation_presented", None)
        else:
            updated_context["request_mode"] = "quote"
            updated_context["equipment_ownership"] = "needs_equipment"
            updated_context["purchase_only"] = purchase_mode == "purchase"
        updated_context = invalidate_changed_facts(context, updated_context)
        try:
            intake = await port.get_service_intake(inbound.business_id, service_id)
        except BookingRequiresHandoff as exc:
            return _handoff_for_reason(str(exc))
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated_context,
            services=services,
            customer_name=customer_name,
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
                "Não consegui identificar o serviço com segurança. "
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
    updated_context = {
        **context,
        "service_id": str(service_id),
    }
    updated_context.pop("service_clarification", None)
    if _service_kind(services, service_id) == "diagnostics":
        raw_issue = " ".join((inbound.body or "").strip().split())
        if raw_issue:
            updated_context["reported_issue"] = raw_issue[:300]
        if any(
            token in normalized
            for token in (
                "barulho",
                "ruido",
                "chiando",
                "estalando",
                "estalo",
                "vibrando",
                "vibracao",
            )
        ):
            updated_context["issue_video_required"] = True
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated_context,
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
                "Não consegui confirmar a quantidade com segurança. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    updated = {
        **_clear_repair_attempt(context, "quantity"),
        "service_id": str(service_id),
        "quantity": quantity,
    }
    if context.get("request_mode") == "quote":
        updated["equipment_quantity"] = quantity
    updated = invalidate_changed_facts(context, updated)
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
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
        normalized = normalize_portuguese(inbound.body or "")
        if any(
            phrase in normalized
            for phrase in (
                "acesso normal",
                "normal",
                "facil",
                "sem dificuldade",
                "sem problema de acesso",
            )
        ):
            access = AccessCondition.NORMAL
        elif any(
            phrase in normalized
            for phrase in (
                "acesso dificil",
                "dificil",
                "complicado",
                "escada",
                "local apertado",
                "dificuldade de acesso",
            )
        ):
            access = AccessCondition.DIFFICULT
        elif normalized in {
            "nao sei",
            "nao tenho certeza",
            "nao consigo informar",
        }:
            access = AccessCondition.UNKNOWN
    if access is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_ACCESS,
            context,
            "access",
            access_selection_message(),
            handoff_body=(
                "Não consegui confirmar a condição de acesso com segurança. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = _clear_repair_attempt(context, "access")
    updated = invalidate_changed_facts(
        context,
        {
            **context,
            "service_id": str(service_id),
            "access_condition": access.value,
        },
    )
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
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
                        "Não consegui confirmar a cidade com segurança. "
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
                    "Não consegui confirmar a cidade com segurança. "
                    "Vou chamar uma pessoa da equipe para continuar com você."
                ),
            )

        if awaiting_city:
            city_alias = _city_alias_from_text(raw_value)
            if city_alias is not None:
                city_name, state_code = city_alias
                address = ServiceAddress(
                    address_line=pending_address,
                    city=city_name,
                    state=state_code,
                )
                return await _advance_intake(
                    inbound,
                    port,
                    intake,
                    _address_success_context(context, service_id, address),
                    customer_name=customer_name,
                )
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
                    "Não consegui confirmar a cidade com segurança. "
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
                    "Não consegui confirmar a cidade com segurança. "
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
                "Ainda preciso de um endereço completo para continuar com segurança. "
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
                "Ainda preciso de um endereço completo para continuar com segurança. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )

    city_alias = _city_alias_from_text(value)
    if city_alias is not None:
        city_name, state_code = city_alias
        address = ServiceAddress(
            address_line=value,
            city=city_name,
            state=state_code,
        )
        return await _advance_intake(
            inbound,
            port,
            intake,
            _address_success_context(context, service_id, address),
            customer_name=customer_name,
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
        updated.pop("pending_address_city_guess", None)
        updated["awaiting_address_city"] = True
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            updated,
            address_request_message(
                "Agora me diga apenas a cidade desse endereço."
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


async def _handle_equipment_ownership(
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
            ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    normalized = normalize_portuguese(inbound.body or "")
    ownership: str | None = None
    if action == EQUIPMENT_HAS or any(
        phrase in normalized
        for phrase in (
            "ja tenho",
            "tenho o aparelho",
            "so instalacao",
            "somente instalacao",
            "apenas instalar",
        )
    ):
        ownership = "has_equipment"
    elif action == EQUIPMENT_NEEDS or any(
        phrase in normalized
        for phrase in (
            "quero cotar",
            "quero comprar",
            "preciso comprar",
            "nao tenho aparelho",
            "nao tenho o ar",
        )
    ):
        ownership = "needs_equipment"

    if ownership is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
            context,
            "equipment_ownership",
            installation_equipment_status_message(),
            handoff_body=(
                "Não consegui confirmar se você já tem o aparelho com segurança. "
                "Vou chamar uma pessoa da equipe para continuar."
            ),
        )

    updated = {
        **_clear_repair_attempt(context, "equipment_ownership"),
        "service_id": str(service_id),
        "equipment_ownership": ownership,
    }
    if ownership == "has_equipment":
        updated["equipment_model_known"] = True
    else:
        updated.setdefault("request_mode", "quote")
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_equipment_model(
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
        services = _snapshot_options(await port.list_services(inbound.business_id))
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_MODEL,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    service_kind = _service_kind(services, service_id)
    existing_equipment = (
        _context_string(context, "equipment_ownership") == "has_equipment"
        or service_kind in {
            "cleaning",
            "gas_recharge",
            "diagnostics",
            "preventive",
        }
    )

    if inbound.message_type == "video":
        if context.get("issue_video_required") is True:
            updated = {
                **context,
                "issue_video_received": True,
            }
            updated.pop("issue_video_requested", None)
            transition = await _advance_intake(
                inbound,
                port,
                intake,
                updated,
                services=services,
                customer_name=customer_name,
            )
            return _prepend_transition_body(
                transition,
                "Recebi o vídeo para o técnico conferir antes do atendimento.",
            )
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_MODEL,
            context,
            media_received_message("video"),
        )

    if inbound.message_type == "image" and existing_equipment:
        updated = {
            **_clear_repair_attempt(context, "equipment_model"),
            "equipment_model_known": False,
            "equipment_photo_received": True,
        }
        updated.pop("equipment_model", None)
        updated.pop("equipment_photo_requested", None)
        transition = await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            services=services,
            customer_name=customer_name,
        )
        return _prepend_transition_body(
            transition,
            "Recebi a foto do aparelho para a equipe conferir.",
        )

    if _context_string(context, "equipment_model") is not None:
        return await _advance_intake(
            inbound,
            port,
            intake,
            context,
            services=services,
            customer_name=customer_name,
        )

    normalized = normalize_portuguese(inbound.body or "")
    unknown_model = (
        action == EQUIPMENT_MODEL_RECOMMEND
        or normalized in {
            "nao",
            "nao tenho",
            "nao sei",
            "nao sei o modelo",
            "nao conheco",
            "sem preferencia",
            "pode recomendar",
        }
    )
    if unknown_model:
        updated = {
            **_clear_repair_attempt(context, "equipment_model"),
            "equipment_model_known": False,
        }
        updated.pop("equipment_model", None)
        if existing_equipment:
            updated["equipment_photo_requested"] = True
            return _transition(
                ConversationState.BOOKING_EQUIPMENT_MODEL,
                updated,
                equipment_photo_request_message(),
            )
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            services=services,
            customer_name=customer_name,
        )

    if action == EQUIPMENT_MODEL_KNOWN and context.get("equipment_model_known") is not True:
        body = (
            "Qual é a marca e o modelo do seu ar-condicionado? "
            "Se não souber, pode me mandar uma foto."
            if existing_equipment
            else "Qual é a marca e o modelo do ar-condicionado?"
        )
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_MODEL,
            {**context, "equipment_model_known": True},
            equipment_model_request_message(body),
        )

    raw = " ".join((inbound.body or "").strip().split())
    if (
        2 <= len(raw) <= 180
        and normalized not in {
            "sim",
            "isso",
            "tenho",
            "ok",
            "certo",
            "nao",
            "nao sei",
        }
        and (
            context.get("equipment_model_known") is True
            or existing_equipment
        )
    ):
        updated = {
            **_clear_repair_attempt(context, "equipment_model"),
            "equipment_model_known": True,
            "equipment_model": raw,
        }
        updated.pop("equipment_photo_requested", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            services=services,
            customer_name=customer_name,
        )

    if action == EQUIPMENT_MODEL_KNOWN:
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_MODEL,
            {**context, "equipment_model_known": True},
            equipment_model_request_message(),
        )

    return _retry_or_handoff(
        ConversationState.BOOKING_EQUIPMENT_MODEL,
        context,
        "equipment_model",
        (
            equipment_model_request_message(
                "Não consegui identificar o modelo. Pode me dizer a marca e o modelo "
                "ou a capacidade em BTUs? Se não souber, diga “não sei”."
            )
            if existing_equipment or context.get("equipment_model_known") is True
            else equipment_model_known_message()
        ),
        handoff_body=(
            "Não consegui confirmar o equipamento com segurança. "
            "Vou chamar uma pessoa da equipe para continuar."
        ),
    )


async def _handle_equipment_profile(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        _service_id, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_PROFILE,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    updated = dict(context)
    raw_text = (inbound.body or "").strip()
    message_facts = enrich_context_from_message({}, raw_text)
    accepted_answer = _has_profile_fact(message_facts)

    preference_by_action = {
        EQUIPMENT_PREF_MODERN: "modern",
        EQUIPMENT_PREF_COST_BENEFIT: "cost_benefit",
        EQUIPMENT_PREF_ECONOMY: "economy",
    }
    if action in preference_by_action:
        updated["equipment_preference"] = preference_by_action[action]
        accepted_answer = True
    if action == EQUIPMENT_CYCLE_COLD:
        updated["equipment_cycle"] = "cold"
        accepted_answer = True
    elif action == EQUIPMENT_CYCLE_HEAT_COOL:
        updated["equipment_cycle"] = "heat_cool"
        accepted_answer = True

    missing_before = missing_equipment_profile_fields(updated)

    if action == EQUIPMENT_SPACE_NO_LIMIT and missing_before:
        if (
            missing_before[0] == "indoor_space"
            and "outdoor_space" in missing_before
        ):
            updated["indoor_space_unrestricted"] = True
            updated["outdoor_space_unrestricted"] = True
            accepted_answer = True
        elif missing_before[0] == "indoor_space":
            updated["indoor_space_unrestricted"] = True
            accepted_answer = True
        elif missing_before[0] == "outdoor_space":
            updated["outdoor_space_unrestricted"] = True
            accepted_answer = True

    updated, parsed = _apply_expected_profile_answer(
        updated,
        missing_before,
        raw_text,
    )
    accepted_answer = accepted_answer or parsed
    updated = invalidate_changed_facts(context, updated)

    missing = missing_equipment_profile_fields(updated)
    updated = _clear_resolved_profile_attempts(updated, missing)

    if missing:
        current_field = missing[0]
        if not accepted_answer and raw_text:
            return _retry_or_handoff(
                ConversationState.BOOKING_EQUIPMENT_PROFILE,
                updated,
                f"equipment_profile:{current_field}",
                _equipment_profile_prompt(missing, retry=True),
                handoff_body=(
                    "Ainda não consegui entender essa informação. "
                    "Vou chamar uma pessoa da nossa equipe para continuar. "
                    "Por favor, aguarde."
                ),
            )
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_PROFILE,
            updated,
            _equipment_profile_prompt(missing),
        )

    try:
        recommended = await _with_equipment_recommendation(
            inbound,
            port,
            updated,
        )
    except ValueError:
        return _handoff_transition(
            "Não encontrei no catálogo ativo um equipamento que atenda "
            "ao ciclo e ao espaço informados. Vou chamar a equipe para validar uma opção."
        )

    recommendation = recommended.get("recommended_equipment")
    label = (
        recommendation.get("label")
        if isinstance(recommendation, dict)
        else None
    )
    recommended = {**recommended, "recommendation_presented": True}
    if isinstance(recommended.get("catalog_miss_count"), int):
        recommended["catalog_alternative_presented"] = True
    transition = await _advance_intake(
        inbound,
        port,
        intake,
        recommended,
        customer_name=customer_name,
    )
    if not isinstance(label, str) or not label:
        return transition

    price = (
        recommendation.get("price")
        if isinstance(recommendation, dict)
        else None
    )
    price_text: str | None = None
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        formatted = f"{float(price):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        price_text = f"R$ {formatted}"

    brand = recommendation.get("brand") if isinstance(recommendation, dict) else None
    line = recommendation.get("line") if isinstance(recommendation, dict) else None
    capacity = (
        recommendation.get("capacity_btu")
        if isinstance(recommendation, dict)
        else None
    )
    cycle = (
        recommendation.get("selected_cycle")
        if isinstance(recommendation, dict)
        else None
    )
    product = " ".join(
        part for part in (brand, line) if isinstance(part, str) and part.strip()
    ) or label
    details: list[str] = [label]
    if not re.search(r"\bbtu\b", normalize_portuguese(label)) and isinstance(
        capacity, int
    ):
        details.append(f"{capacity:,}".replace(",", ".") + " BTU")
    if cycle in {"cold", "heat_cool"} and "frio" not in normalize_portuguese(label):
        details.append("ciclo só frio" if cycle == "cold" else "ciclo quente/frio")
    intro_body = "Uma boa referência é " + ", ".join(details)
    intro_body += f", por {price_text}." if price_text else ", com valor a confirmar."
    within_budget = (
        recommendation.get("within_budget")
        if isinstance(recommendation, dict)
        else None
    )
    recommendation_budget = (
        recommendation.get("budget_max")
        if isinstance(recommendation, dict)
        else None
    )
    if (
        within_budget is False
        and isinstance(recommendation_budget, (int, float))
        and not isinstance(recommendation_budget, bool)
    ):
        intro_body = (
            "Não encontrei uma opção compatível dentro de "
            f"{_format_brl(Decimal(str(recommendation_budget)))}. "
            "A alternativa compatível mais próxima é "
            + ", ".join(details)
        )
        intro_body += f", por {price_text}." if price_text else ", com valor a confirmar."
    intro = _text_message(intro_body)

    followups: list[OutboundMessage] = []
    image_url = (
        recommendation.get("image_url")
        if isinstance(recommendation, dict)
        else None
    )
    if isinstance(image_url, str) and image_url.startswith("https://"):
        caption = product
        followups.append(
            equipment_image_message(
                image_url,
                caption,
            )
        )
    followups.extend((transition.outbound, *transition.follow_ups))
    return replace(
        transition,
        outbound=intro,
        follow_ups=tuple(followups),
    )


async def _handle_equipment_delivery(
    inbound: ConversationInput,
    context: dict[str, Any],
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
        _, intake = await _context_intake(inbound, context, port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_EQUIPMENT_DELIVERY,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    normalized = correction_focus(inbound.body or "")
    if action is None:
        if normalized in {"retirar", "buscar", "vou retirar", "retirada"}:
            action = EQUIPMENT_DELIVERY_PICKUP
        elif normalized in {"receber", "entregar", "entrega", "quero receber"}:
            action = EQUIPMENT_DELIVERY_ADDRESS
        elif normalized in {
            "levar com a instalacao",
            "levar junto com a instalacao",
            "junto com o tecnico",
            "no dia da instalacao",
            "com a instalacao",
        }:
            action = EQUIPMENT_DELIVERY_WITH_INSTALLATION
        elif normalized in {"sim", "mesmo endereco", "no mesmo endereco"}:
            action = EQUIPMENT_INSTALLATION_SAME_ADDRESS
        elif normalized in {"nao", "outro endereco", "endereco diferente"}:
            action = EQUIPMENT_INSTALLATION_OTHER_ADDRESS

    updated = _clear_repair_attempt(context, "equipment_delivery")
    if action == EQUIPMENT_DELIVERY_PICKUP:
        updated["delivery_method"] = "pickup"
        updated.pop("delivery_address", None)
        updated.pop("delivery_installation_match_pending", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )
    if (
        action == EQUIPMENT_DELIVERY_WITH_INSTALLATION
        and context.get("purchase_mode") == "both"
    ):
        updated["delivery_method"] = "with_installation"
        updated.pop("delivery_address", None)
        updated.pop("address_purpose", None)
        updated.pop("delivery_installation_match_pending", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )
    if action == EQUIPMENT_DELIVERY_ADDRESS:
        updated["delivery_method"] = "delivery"
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            {**updated, "address_purpose": "delivery"},
            address_request_message(
                "Qual é o endereço de entrega, com rua, número e cidade?"
            ),
        )
    if action == EQUIPMENT_INSTALLATION_SAME_ADDRESS:
        delivery_address = ServiceAddress.from_snapshot(
            updated.get("delivery_address")
        )
        if delivery_address is None:
            return _transition(
                ConversationState.BOOKING_ADDRESS,
                {**updated, "address_purpose": "delivery"},
                address_request_message(
                    "Qual é o endereço de entrega, com rua, número e cidade?"
                ),
            )
        updated["service_address"] = delivery_address.to_snapshot()
        updated.pop("delivery_installation_match_pending", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )
    if action == EQUIPMENT_INSTALLATION_OTHER_ADDRESS:
        updated.pop("delivery_installation_match_pending", None)
        return await _advance_intake(
            inbound,
            port,
            intake,
            updated,
            customer_name=customer_name,
        )

    pending_match = context.get("delivery_installation_match_pending") is True
    prompt = (
        delivery_installation_address_message()
        if pending_match
        else equipment_delivery_message(
            include_with_installation=context.get("purchase_mode") == "both"
        )
    )
    return _retry_or_handoff(
        ConversationState.BOOKING_EQUIPMENT_DELIVERY,
        context,
        "equipment_delivery",
        prompt,
        handoff_body=(
            "Preciso que a equipe confirme a forma de entrega com você. "
            "Vou encaminhar essa parte do atendimento."
        ),
    )


def _is_completion_acknowledgement(value: str | None) -> bool:
    normalized = normalize_portuguese(value or "").strip()
    if not normalized:
        return False
    exact = {
        "obrigado",
        "obrigada",
        "muito obrigado",
        "muito obrigada",
        "valeu",
        "ate logo",
        "tchau",
        "falou",
        "blz",
        "beleza",
    }
    if normalized in exact:
        return True
    return any(
        normalized.startswith(prefix)
        for prefix in (
            "obrigado ",
            "obrigada ",
            "valeu ",
            "ate logo ",
            "tchau ",
        )
    )


def _has_profile_fact(context: dict[str, Any]) -> bool:
    keys = {
        "room_people_max",
        "room_area_m2",
        "equipment_preference",
        "equipment_cycle",
        "indoor_space_width_cm",
        "indoor_space_height_cm",
        "indoor_space_unrestricted",
        "outdoor_space_width_cm",
        "outdoor_space_height_cm",
        "outdoor_space_unrestricted",
    }
    return any(key in context for key in keys)


def _apply_expected_profile_answer(
    context: dict[str, Any],
    missing: Sequence[str],
    body: str,
) -> tuple[dict[str, Any], bool]:
    if not missing or not body.strip():
        return context, False

    updated = dict(context)
    field = missing[0]
    normalized = normalize_portuguese(body)
    number = parse_number_answer(body)

    if field == "people":
        if (
            number is not None
            and number.is_integer()
            and 1 <= number <= 100
            and not re.search(r"\b(?:m2|metro|metros|cm|centimetro|centimetros)\b", normalized)
        ):
            updated["room_people_max"] = int(number)
            return updated, True
        return updated, False

    if field == "area":
        if (
            number is not None
            and 1 <= number <= 500
            and not re.search(r"\b(?:pessoa|pessoas|ocupantes)\b", normalized)
        ):
            updated["room_area_m2"] = number
            return updated, True
        return updated, False

    if field in {"indoor_space", "outdoor_space"}:
        if _unrestricted_space_answer(normalized):
            if field == "indoor_space" and "outdoor_space" in missing:
                updated["indoor_space_unrestricted"] = True
                updated["outdoor_space_unrestricted"] = True
            else:
                target = "indoor" if field == "indoor_space" else "outdoor"
                updated[f"{target}_space_unrestricted"] = True
            return updated, True

        both = _labeled_space_dimensions(body)
        if both:
            for target, dimensions in both.items():
                width, height, depth = dimensions
                updated[f"{target}_space_width_cm"] = width
                updated[f"{target}_space_height_cm"] = height
                if depth is not None:
                    updated[f"{target}_space_depth_cm"] = depth
                updated[f"{target}_space_unrestricted"] = False
            return updated, True

        values = [
            float(value.replace(",", "."))
            for value in re.findall(r"(\d{1,3}(?:[.,]\d{1,2})?)", body)
        ]
        if len(values) >= 2 and all(1 <= value <= 500 for value in values[:3]):
            target = "indoor" if field == "indoor_space" else "outdoor"
            updated[f"{target}_space_width_cm"] = values[0]
            updated[f"{target}_space_height_cm"] = values[1]
            if len(values) >= 3:
                updated[f"{target}_space_depth_cm"] = values[2]
            updated[f"{target}_space_unrestricted"] = False
            return updated, True

    return updated, False


def _unrestricted_space_answer(normalized: str) -> bool:
    exact = {
        "nao",
        "nenhuma",
        "nenhum",
        "sem restricao",
        "sem limitacao",
        "nao tem",
        "nao tenho",
        "nao ha",
        "espaco livre",
        "tem espaco",
        "tem bastante espaco",
    }
    if normalized in exact:
        return True
    return any(
        phrase in normalized
        for phrase in (
            "nao tem limitacao",
            "nao tenho limitacao",
            "nao tem restricao",
            "nao tenho restricao",
            "nao ha limitacao",
            "nao ha restricao",
            "sem problema de espaco",
        )
    )


def _labeled_space_dimensions(
    body: str,
) -> dict[str, tuple[float, float, float | None]]:
    prepared = re.sub(r"(?<=\d)[x×](?=\d)", " x ", body.casefold())
    normalized = normalize_portuguese(prepared)
    labels = {
        "indoor": r"(?:unidade interna|parte interna|evaporadora|interna)",
        "outdoor": r"(?:unidade externa|parte externa|condensadora|externa)",
    }
    result: dict[str, tuple[float, float, float | None]] = {}
    for target, label in labels.items():
        match = re.search(
            rf"{label}.{{0,45}}?(\d{{1,3}}(?:[.,]\d{{1,2}})?)"
            rf"\s*(?:x|por|cm)?\s*"
            rf"(\d{{1,3}}(?:[.,]\d{{1,2}})?)"
            rf"(?:\s*(?:x|por|cm)?\s*(\d{{1,3}}(?:[.,]\d{{1,2}})?))?",
            normalized,
        )
        if not match:
            continue
        values = [
            float(value.replace(",", "."))
            for value in match.groups()
            if value is not None
        ]
        if len(values) >= 2 and all(1 <= value <= 500 for value in values):
            result[target] = (
                values[0],
                values[1],
                values[2] if len(values) >= 3 else None,
            )
    return result


def _equipment_profile_prompt(
    missing: Sequence[str],
    *,
    retry: bool = False,
) -> OutboundMessage:
    if not missing:
        return equipment_profile_message(missing)
    field = missing[0]
    if field == "preference":
        return equipment_preference_message(retry=retry)
    if field == "cycle":
        return equipment_cycle_message(retry=retry)
    if field == "indoor_space":
        target = "both" if "outdoor_space" in missing else "indoor"
        return equipment_space_message(target, retry=retry)
    if field == "outdoor_space":
        return equipment_space_message("outdoor", retry=retry)
    return equipment_profile_message(missing, retry=retry)


def _clear_resolved_profile_attempts(
    context: dict[str, Any],
    missing: Sequence[str],
) -> dict[str, Any]:
    updated = dict(context)
    attempts = _repair_attempts(updated)
    unresolved = set(missing)
    for field in (
        "people",
        "area",
        "preference",
        "cycle",
        "indoor_space",
        "outdoor_space",
    ):
        if field not in unresolved:
            attempts.pop(f"equipment_profile:{field}", None)
    if attempts:
        updated["repair_attempts"] = attempts
    else:
        updated.pop("repair_attempts", None)
    return updated


async def _handle_installation_height(
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
            ConversationState.BOOKING_INSTALLATION_HEIGHT,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    height: bool | None = None
    normalized = normalize_portuguese(inbound.body or "")
    if action == HEIGHT_OVER_3M:
        height = True
    elif action == HEIGHT_AT_MOST_3M:
        height = False
    elif any(
        phrase in normalized
        for phrase in ("mais de 3", "acima de 3", "passa de 3")
    ):
        height = True
    elif any(
        phrase in normalized
        for phrase in ("ate 3", "menos de 3", "abaixo de 3", "nao passa de 3")
    ):
        height = False
    else:
        value = _decimal_from_text(inbound.body)
        if value is not None:
            height = value > Decimal("3")

    if height is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_INSTALLATION_HEIGHT,
            context,
            "installation_height",
            installation_height_message(),
            handoff_body=(
                "Não consegui confirmar a altura da instalação com segurança. "
                "Vou chamar uma pessoa da equipe para continuar."
            ),
        )

    updated = {
        **_clear_repair_attempt(context, "installation_height"),
        "service_id": str(service_id),
        "installation_height_over_3m": height,
        "work_at_height": height,
        "tubing_length_answered": True,
    }
    updated = invalidate_changed_facts(context, updated)
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_property_type(
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
            ConversationState.BOOKING_PROPERTY,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    normalized = correction_focus(inbound.body or "")
    mapping = {
        PROPERTY_HOUSE: "house",
        PROPERTY_BUILDING: "building",
        PROPERTY_CONDOMINIUM: "condominium",
    }
    property_type = mapping.get(action)
    if property_type is None:
        if "condominio" in normalized:
            property_type = "condominium"
        elif any(token in normalized for token in ("predio", "edificio", "apartamento", "apto")):
            property_type = "building"
        elif any(token in normalized for token in ("casa", "residencia")):
            property_type = "house"

    if property_type is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_PROPERTY,
            context,
            "property_type",
            property_type_message(),
            handoff_body=(
                "Não consegui confirmar o tipo de local com segurança. "
                "Vou chamar uma pessoa da equipe para continuar."
            ),
        )

    updated = invalidate_changed_facts(context, {
        **_clear_repair_attempt(context, "property_type"),
        "service_id": str(service_id),
        "property_type": property_type,
    })
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_building_hours(
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
            ConversationState.BOOKING_BUILDING_HOURS,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    start = _context_string(context, "building_hours_start")
    end = _context_string(context, "building_hours_end")
    if start is None or end is None:
        window = _time_window_from_text(inbound.body)
        if window is not None:
            start, end = window

    if start is None or end is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_BUILDING_HOURS,
            context,
            "building_hours",
            building_hours_message(retry=True),
            handoff_body=(
                "Ainda não consegui entender o horário permitido no prédio/condomínio. "
                "Vou chamar uma pessoa da nossa equipe para continuar. "
                "Por favor, aguarde."
            ),
        )

    updated = {
        **_clear_repair_attempt(context, "building_hours"),
        "service_id": str(service_id),
        "building_hours_start": start,
        "building_hours_end": end,
        "site_allowed_end": end,
        "site_limit_answered": True,
    }
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
        customer_name=customer_name,
    )


async def _handle_gate_details(
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
            ConversationState.BOOKING_GATE_DETAILS,
            context,
            booking_unavailable_message(),
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    value = " ".join((inbound.body or "").strip().split())
    if len(value) < 2 or len(value) > 300:
        return _retry_or_handoff(
            ConversationState.BOOKING_GATE_DETAILS,
            context,
            "gate_details",
            gate_details_message(),
            handoff_body=(
                "Não consegui confirmar os dados da portaria com segurança. "
                "Vou chamar uma pessoa da equipe para continuar."
            ),
        )

    updated = {
        **_clear_repair_attempt(context, "gate_details"),
        "service_id": str(service_id),
        "gate_instructions": value,
    }
    return await _advance_intake(
        inbound,
        port,
        intake,
        updated,
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
                "Não consegui confirmar a metragem com segurança. "
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
                "Não consegui confirmar o horário limite com segurança. "
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
                "Não consegui confirmar o dia da semana com segurança. "
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
                    "Não consegui confirmar a data com segurança. "
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
                "Não consegui confirmar a data com segurança. "
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
                "Não consegui confirmar a data com segurança. "
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
                "Não consegui confirmar o horário com segurança. "
                "Vou chamar uma pessoa da equipe para continuar com você."
            ),
        )
    context = {
        **_clear_repair_attempt(context, "time"),
        "selected_date": selected_date,
        "selected_time": selected_time,
    }
    return await _advance_preconfirmation(
        inbound,
        port,
        context,
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
                "Não consegui confirmar sua decisão com segurança. "
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
        ConversationState.POST_BOOKING_HELP,
        _clean_context(context),
        booking_completed_message(
            f"Agendamento confirmado para {date_short_label(selected_date)} "
            f"às {selected_time}."
        ),
        follow_ups=(post_booking_help_message(),),
    )


async def _handle_post_booking_help(
    conversation: ConversationSnapshot,
    inbound: ConversationInput,
    interpretation: Interpretation,
    action: str | None,
    booking_port: BookingAvailabilityPort | None,
) -> ConversationTransition:
    normalized = normalize_portuguese(inbound.body or "")
    no_more_help = (
        action == POST_BOOKING_HELP_NO
        or normalized in {
            "nao",
            "nao obrigado",
            "nao obrigada",
            "nao preciso",
            "era so isso",
            "so isso",
            "tudo certo",
            "obrigado",
            "obrigada",
            "muito obrigado",
            "muito obrigada",
        }
    )
    if no_more_help:
        return _transition(
            ConversationState.COMPLETED,
            _clean_context(conversation.context),
            farewell_message(),
        )

    if action == POST_BOOKING_HELP_YES or normalized in {
        "sim",
        "sim preciso",
        "preciso",
        "quero",
    }:
        return _transition(
            ConversationState.MENU,
            {},
            _text_message(
                "Claro. Me diga o que mais você precisa e continuo por aqui."
            ),
        )

    supported = {
        ConversationIntent.BOOK,
        ConversationIntent.AVAILABILITY,
        ConversationIntent.SERVICE_INTENT,
        ConversationIntent.EQUIPMENT_PURCHASE,
        ConversationIntent.PRICE_QUESTION,
        ConversationIntent.DURATION_QUESTION,
        ConversationIntent.SERVICE_QUESTION,
        ConversationIntent.RESCHEDULE,
        ConversationIntent.CANCEL,
    }
    if any(interpretation.has(intent) for intent in supported):
        restarted = replace(
            conversation,
            state=ConversationState.START.value,
            context={},
        )
        return await _handle_natural_start(
            restarted,
            inbound,
            interpretation,
            booking_port,
        )

    if isinstance(inbound.body, str) and inbound.body.strip():
        return _handoff_transition(
            "Esse assunto precisa de uma pessoa da equipe para continuar. "
            "Estou encaminhando seu atendimento."
        )

    return _transition(
        ConversationState.POST_BOOKING_HELP,
        {},
        post_booking_help_message(),
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
    service_id = _context_service_id(context)
    if service_id is None:
        return await _restart_service_selection(inbound, port)

    available_services = services or _snapshot_options(
        await port.list_services(inbound.business_id)
    )
    service_kind = _service_kind(available_services, service_id)

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
        and not (
            service_kind == "installation"
            and (
                _context_string(context, "equipment_ownership") == "needs_equipment"
                or context.get("purchase_mode") in {"purchase", "both"}
            )
        )
    ):
        return _transition(
            ConversationState.BOOKING_ADDRESS,
            context,
            address_request_message(),
        )

    if service_kind in {
        "cleaning",
        "gas_recharge",
        "diagnostics",
        "preventive",
    }:
        context = {
            **context,
            "equipment_ownership": "has_equipment",
        }
        has_equipment_reference = (
            _context_string(context, "equipment_model") is not None
            or context.get("equipment_photo_received") is True
        )
        if not has_equipment_reference:
            if context.get("equipment_model_known") is False:
                updated = {
                    **context,
                    "equipment_photo_requested": True,
                }
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_MODEL,
                    updated,
                    equipment_photo_request_message(),
                )
            updated = {
                **context,
                "equipment_model_known": True,
            }
            return _transition(
                ConversationState.BOOKING_EQUIPMENT_MODEL,
                updated,
                equipment_model_request_message(
                    "Qual é a marca e o modelo do seu ar-condicionado? "
                    "Se não souber, pode me mandar uma foto."
                ),
            )

        if (
            context.get("issue_video_required") is True
            and context.get("issue_video_received") is not True
        ):
            updated = {
                **context,
                "issue_video_requested": True,
            }
            return _transition(
                ConversationState.BOOKING_EQUIPMENT_MODEL,
                updated,
                diagnostic_noise_video_request_message(),
            )

    if service_kind == "installation":
        ownership = _context_string(context, "equipment_ownership")
        if (
            ownership not in {"has_equipment", "needs_equipment"}
            and isinstance(context.get("purchase_only"), bool)
        ):
            ownership = "needs_equipment"
            context = {
                **context,
                "equipment_ownership": ownership,
            }
        if ownership not in {"has_equipment", "needs_equipment"}:
            return _transition(
                ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
                context,
                installation_equipment_status_message(),
            )

        if ownership == "has_equipment":
            has_equipment_reference = (
                _context_string(context, "equipment_model") is not None
                or context.get("equipment_photo_received") is True
            )
            if not has_equipment_reference:
                if context.get("equipment_model_known") is False:
                    updated = {
                        **context,
                        "equipment_photo_requested": True,
                    }
                    return _transition(
                        ConversationState.BOOKING_EQUIPMENT_MODEL,
                        updated,
                        equipment_photo_request_message(),
                    )
                updated = {
                    **context,
                    "equipment_model_known": True,
                }
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_MODEL,
                    updated,
                    equipment_model_request_message(
                        "Qual é a marca e o modelo do seu ar-condicionado? "
                        "Se não souber, pode me mandar uma foto."
                    ),
                )
        else:
            model_known = context.get("equipment_model_known")
            model_text = _context_string(context, "equipment_model")
            if (
                "recommended_equipment" not in context
                and model_known is None
                and model_text is None
            ):
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_MODEL,
                    context,
                    equipment_model_known_message(),
                )
            if (
                "recommended_equipment" not in context
                and model_known is True
                and model_text is None
            ):
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_MODEL,
                    context,
                    equipment_model_request_message(),
                )
            if "recommended_equipment" not in context:
                # A model typed by the customer is a preference/request, not a
                # recommendation. Purchased equipment must always be dimensioned
                # and selected from the active catalog before quote/checkout.
                profile_context = {
                    **context,
                    "equipment_model_known": False,
                }
                missing = missing_equipment_profile_fields(profile_context)
                if missing:
                    prompt = _equipment_profile_prompt(missing)
                    if profile_context.get("equipment_profile_intro_sent") is not True:
                        updated = {
                            **profile_context,
                            "equipment_profile_intro_sent": True,
                        }
                        return _transition(
                            ConversationState.BOOKING_EQUIPMENT_PROFILE,
                            updated,
                            equipment_profile_intro_message(),
                            follow_ups=(prompt,),
                        )
                    return _transition(
                        ConversationState.BOOKING_EQUIPMENT_PROFILE,
                        profile_context,
                        prompt,
                    )
                return await _handle_equipment_profile(
                    replace(inbound, body=None),
                    profile_context,
                    None,
                    port,
                    customer_name=customer_name,
                )

        if (
            ownership == "needs_equipment"
            and "recommended_equipment" in context
            and context.get("recommendation_presented") is True
            and isinstance(context.get("purchase_only"), bool)
        ):
            if _context_string(context, "delivery_method") is None:
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_DELIVERY,
                    context,
                    equipment_delivery_message(
                        include_with_installation=context.get("purchase_mode") == "both"
                    ),
                )
            if (
                context.get("delivery_method") == "delivery"
                and ServiceAddress.from_snapshot(context.get("delivery_address")) is None
            ):
                return _transition(
                    ConversationState.BOOKING_ADDRESS,
                    {**context, "address_purpose": "delivery"},
                    address_request_message(
                        "Qual é o endereço de entrega, com rua, número e cidade?"
                    ),
                )
            if context.get("delivery_installation_match_pending") is True:
                return _transition(
                    ConversationState.BOOKING_EQUIPMENT_DELIVERY,
                    context,
                    delivery_installation_address_message(),
                )
            if context.get("purchase_only") is True:
                recommendation = context.get("recommended_equipment")
                purchase_lines = [
                    "Perfeito. Segue o resumo da compra:",
                ]
                if isinstance(recommendation, dict):
                    label = recommendation.get("label")
                    price = recommendation.get("price")
                    if isinstance(label, str) and label:
                        purchase_lines.append(f"• Equipamento: {label}")
                    if isinstance(price, (int, float)) and not isinstance(price, bool):
                        purchase_lines.append(
                            f"• Valor do equipamento: {_format_brl(Decimal(str(price)))}"
                        )
                purchase_lines.append(
                    "A equipe seguirá com a confirmação de disponibilidade e os próximos detalhes da compra."
                )
                return _transition(
                    ConversationState.COMPLETED,
                    context,
                    _text_message("\n".join(purchase_lines)),
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

        if context.get("installation_height_over_3m") is None:
            return _transition(
                ConversationState.BOOKING_INSTALLATION_HEIGHT,
                context,
                installation_height_message(),
            )

        if context.get("tube_disclaimer_sent") is not True:
            details = await _safe_service_details(
                inbound,
                port,
                service_id,
            )
            updated = {
                **context,
                "tube_disclaimer_sent": True,
                "tubing_length_answered": True,
            }
            next_transition = await _advance_intake(
                inbound,
                port,
                intake,
                updated,
                services=available_services,
                customer_name=customer_name,
            )
            return replace(
                next_transition,
                outbound=tubing_variation_message(
                    getattr(details, "included_tubing_meters", None),
                    getattr(details, "extra_tubing_price", None),
                ),
                follow_ups=(
                    next_transition.outbound,
                    *next_transition.follow_ups,
                ),
            )

    if (
        service_kind != "installation"
        and intake.asks_tubing_length
        and context.get("tubing_length_answered") is not True
    ):
        return await _tubing_prompt_transition(
            inbound,
            port,
            context,
            service_id,
        )

    needs_property = (
        service_kind == "installation"
        or context.get("request_mode") == "quote"
    )
    if needs_property and _context_string(context, "property_type") is None:
        return _transition(
            ConversationState.BOOKING_PROPERTY,
            context,
            property_type_message(),
        )

    property_type = _context_string(context, "property_type")
    if needs_property and property_type in {"building", "condominium"}:
        if (
            _context_string(context, "building_hours_start") is None
            or _context_string(context, "building_hours_end") is None
        ):
            return _transition(
                ConversationState.BOOKING_BUILDING_HOURS,
                context,
                building_hours_message(),
            )
        if _context_string(context, "gate_instructions") is None:
            return _transition(
                ConversationState.BOOKING_GATE_DETAILS,
                context,
                gate_details_message(),
            )

    if intake.asks_site_time_limit and context.get("site_limit_answered") is not True:
        if property_type in {"building", "condominium"} and _context_string(
            context, "building_hours_end"
        ):
            context = {
                **context,
                "site_limit_answered": True,
                "site_allowed_end": _context_string(
                    context,
                    "building_hours_end",
                ),
            }
        else:
            return _transition(
                ConversationState.BOOKING_SITE_LIMIT,
                context,
                site_limit_message(),
            )

    if (
        context.get("request_mode") == "quote"
        and context.get("quote_presented") is not True
    ):
        return await _quote_transition(
            inbound,
            port,
            context,
            service_id,
            customer_name=customer_name,
        )

    return await _offer_dates(
        inbound,
        port,
        context,
        services=available_services,
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

    estimate = (
        ""
        if context.get("quote_presented") is True
        else _estimate_message(plan)
    )
    prefix = f"{estimate}\n\n" if estimate else ""
    if len(dates) > 10:
        body = (
            f"{prefix}Tenho disponibilidade em vários dias. "
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
        f"{prefix}Tenho disponibilidade nestas datas. "
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
        return await _advance_preconfirmation(
            inbound,
            port,
            {
                **context,
                "selected_date": selected_date,
                "selected_time": selected_time,
            },
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



async def _handle_attendee(
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
            ConversationState.BOOKING_ATTENDEE,
            context,
            booking_unavailable_message(),
        )

    normalized = normalize_portuguese(inbound.body or "")
    mode = _context_string(context, "onsite_contact_mode")
    if action == ATTENDEE_CUSTOMER or normalized in {
        "sim",
        "sou eu",
        "eu vou estar",
        "eu estarei",
        "eu mesmo",
        "eu mesma",
    }:
        mode = "customer"
    elif action == ATTENDEE_OTHER or any(
        phrase in normalized
        for phrase in ("outra pessoa", "nao", "nao vou estar")
    ):
        mode = "other"

    if mode not in {"customer", "other"}:
        return _retry_or_handoff(
            ConversationState.BOOKING_ATTENDEE,
            context,
            "attendee",
            attendee_message(customer_name),
            handoff_body=(
                "Não consegui confirmar quem estará no local com segurança. "
                "Vou chamar uma pessoa da equipe para continuar."
            ),
        )

    updated = {
        **_clear_repair_attempt(context, "attendee"),
        "onsite_contact_mode": mode,
    }
    if mode == "customer":
        if customer_name:
            updated["onsite_contact_name"] = customer_name
        return await _advance_preconfirmation(
            inbound,
            port,
            updated,
            customer_name=customer_name,
        )
    if _context_string(updated, "onsite_contact_name") is None:
        return _transition(
            ConversationState.BOOKING_ATTENDEE_NAME,
            updated,
            attendee_name_message(),
        )
    return await _advance_preconfirmation(
        inbound,
        port,
        updated,
        customer_name=customer_name,
    )


async def _handle_attendee_name(
    inbound: ConversationInput,
    context: dict[str, Any],
    booking_port: BookingAvailabilityPort | None,
    *,
    customer_name: str | None = None,
) -> ConversationTransition:
    try:
        port = _require_booking_port(booking_port)
    except BookingPortUnavailable:
        return _transition(
            ConversationState.BOOKING_ATTENDEE_NAME,
            context,
            booking_unavailable_message(),
        )

    name = extract_customer_name(inbound.body, allow_bare=True)
    if name is None:
        return _retry_or_handoff(
            ConversationState.BOOKING_ATTENDEE_NAME,
            context,
            "attendee_name",
            attendee_name_message(),
            handoff_body=(
                "Não consegui confirmar o nome da pessoa que estará no local "
                "com segurança. Vou chamar a equipe para continuar."
            ),
        )
    updated = {
        **_clear_repair_attempt(context, "attendee_name"),
        "onsite_contact_mode": "other",
        "onsite_contact_name": name,
    }
    return await _advance_preconfirmation(
        inbound,
        port,
        updated,
        customer_name=customer_name,
    )


async def _handle_phone_confirmation(
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
            ConversationState.BOOKING_PHONE_CONFIRM,
            context,
            booking_unavailable_message(),
        )

    normalized = normalize_portuguese(inbound.body or "")
    whatsapp_phone = _context_string(context, "whatsapp_contact_phone")
    if action == PHONE_CONFIRM or normalized in {"sim", "pode", "correto", "isso"}:
        if whatsapp_phone is None:
            return _transition(
                ConversationState.BOOKING_PHONE_CONFIRM,
                {**context, "awaiting_other_phone": True},
                phone_request_message(),
            )
        updated = {
            **_clear_repair_attempt(context, "phone"),
            "contact_phone": whatsapp_phone,
            "contact_phone_confirmed": True,
        }
        updated.pop("awaiting_other_phone", None)
        return await _resume_final_confirmation(
            inbound,
            port,
            updated,
            customer_name=customer_name,
        )

    if action == PHONE_OTHER or normalized in {
        "outro",
        "outro numero",
        "nao",
    }:
        return _transition(
            ConversationState.BOOKING_PHONE_CONFIRM,
            {**context, "awaiting_other_phone": True},
            phone_request_message(),
        )

    if context.get("awaiting_other_phone") is True:
        phone = _phone_from_text(inbound.body)
        if phone is not None:
            updated = {
                **_clear_repair_attempt(context, "phone"),
                "contact_phone": phone,
                "contact_phone_confirmed": True,
            }
            updated.pop("awaiting_other_phone", None)
            return await _resume_final_confirmation(
                inbound,
                port,
                updated,
                customer_name=customer_name,
            )

    phone = _context_string(context, "contact_phone")
    if phone is not None and phone != whatsapp_phone:
        updated = {
            **_clear_repair_attempt(context, "phone"),
            "contact_phone": phone,
            "contact_phone_confirmed": True,
        }
        return await _resume_final_confirmation(
            inbound,
            port,
            updated,
            customer_name=customer_name,
        )

    return _retry_or_handoff(
        ConversationState.BOOKING_PHONE_CONFIRM,
        context,
        "phone",
        (
            phone_confirmation_message(whatsapp_phone)
            if whatsapp_phone
            else phone_request_message()
        ),
        handoff_body=(
            "Não consegui confirmar o telefone de contato com segurança. "
            "Vou chamar uma pessoa da equipe para continuar."
        ),
    )


async def _handle_quote_decision(
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
            ConversationState.QUOTE_DECISION,
            context,
            booking_unavailable_message(),
        )
    normalized = normalize_portuguese(inbound.body or "")
    if action is None:
        if any(
            phrase in normalized
            for phrase in ("consultar agenda", "agendar", "quero marcar", "ver horario")
        ):
            action = QUOTE_SCHEDULE
        elif any(
            phrase in normalized
            for phrase in ("so cotacao", "so queria cotacao", "obrigado", "era isso")
        ):
            action = QUOTE_FINISH

    if action == QUOTE_FINISH:
        updated = {
            **context,
            "quote_presented": True,
            "quote_paused": True,
        }
        return _transition(
            ConversationState.COMPLETED,
            updated,
            _text_message(
                "Perfeito. A cotação fica registrada nesta conversa. "
                "Quando quiser avançar, é só me chamar."
            ),
        )
    if action == QUOTE_SCHEDULE:
        updated = {
            **context,
            "quote_presented": True,
        }
        updated.pop("quote_paused", None)
        updated.pop("request_mode", None)
        return await _offer_dates(
            inbound,
            port,
            updated,
            customer_name=customer_name,
        )
    return _retry_or_handoff(
        ConversationState.QUOTE_DECISION,
        context,
        "quote_decision",
        quote_decision_message(
            "Gostaria de já agendar a instalação?"
        ),
        handoff_body=(
            "Não consegui entender se você quer seguir para o agendamento. "
            "Vou chamar uma pessoa da equipe para continuar."
        ),
    )


async def _advance_preconfirmation(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    *,
    customer_name: str | None,
) -> ConversationTransition:
    mode = _context_string(context, "onsite_contact_mode")
    if mode not in {"customer", "other"}:
        return _transition(
            ConversationState.BOOKING_ATTENDEE,
            context,
            attendee_message(customer_name),
        )
    if mode == "other" and _context_string(context, "onsite_contact_name") is None:
        return _transition(
            ConversationState.BOOKING_ATTENDEE_NAME,
            context,
            attendee_name_message(),
        )
    if context.get("contact_phone_confirmed") is not True:
        phone = (
            _context_string(context, "contact_phone")
            or _context_string(context, "whatsapp_contact_phone")
        )
        if phone is None:
            return _transition(
                ConversationState.BOOKING_PHONE_CONFIRM,
                {**context, "awaiting_other_phone": True},
                phone_request_message(),
            )
        return _transition(
            ConversationState.BOOKING_PHONE_CONFIRM,
            context,
            phone_confirmation_message(phone),
        )
    return await _resume_final_confirmation(
        inbound,
        port,
        context,
        customer_name=customer_name,
    )


async def _resume_final_confirmation(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    *,
    customer_name: str | None,
) -> ConversationTransition:
    booking_data = _booking_data(context)
    if booking_data is None:
        return await _restart_service_selection(inbound, port)
    service_id, selected_date, selected_time = booking_data
    return await _booking_confirm_transition(
        inbound,
        port,
        context,
        service_id,
        selected_date,
        selected_time,
        _requirements_from_context(context),
        customer_name=customer_name,
    )


async def _quote_transition(
    inbound: ConversationInput,
    port: BookingAvailabilityPort,
    context: dict[str, Any],
    service_id: uuid.UUID,
    *,
    customer_name: str | None,
) -> ConversationTransition:
    requirements = _requirements_from_context(context)
    try:
        plan = await port.estimate(
            inbound.business_id,
            service_id,
            requirements,
        )
    except BookingRequiresHandoff as exc:
        return _handoff_for_reason(str(exc))

    services = _snapshot_options(await port.list_services(inbound.business_id))
    selected = next(
        (item for item in services if item.id == str(service_id)),
        None,
    )
    service_label = selected.label if selected is not None else "serviço selecionado"
    recommendation = context.get("recommended_equipment")

    equipment_lines: list[str] = []
    if (
        isinstance(recommendation, dict)
        and context.get("recommendation_presented") is not True
    ):
        label = recommendation.get("label")
        if isinstance(label, str) and label:
            equipment_lines.append(f"Equipamento sugerido: {label}.")
            equipment_price = recommendation.get("price")
            if isinstance(equipment_price, (int, float)) and not isinstance(
                equipment_price, bool
            ):
                equipment_lines.append(
                    f"Equipamento: {_format_brl(Decimal(str(equipment_price)))}."
                )
            else:
                equipment_lines.append(
                    "Equipamento: valor a confirmar conforme preço e estoque da empresa."
                )
    model = _context_string(context, "equipment_model")
    if model:
        equipment_lines.append(f"Equipamento informado: {model}.")

    service_kind = _service_kind(services, service_id)
    service_amount = plan.service.estimated_price
    equipment_amount: Decimal | None = None
    if isinstance(recommendation, dict):
        raw_equipment_price = recommendation.get("price")
        if isinstance(raw_equipment_price, (int, float)) and not isinstance(
            raw_equipment_price, bool
        ):
            equipment_amount = Decimal(str(raw_equipment_price))

    financial_lines: list[str] = []
    if service_amount is not None:
        service_price_label = (
            "Valor base do serviço técnico"
            if service_kind == "diagnostics"
            else "Valor do serviço"
        )
        financial_lines.append(
            f"{service_price_label}: {_format_brl(service_amount)}."
        )
    else:
        financial_lines.append(
            "Valor do serviço: a confirmar após validar a configuração."
        )
    if context.get("purchase_mode") in {"purchase", "both"} and equipment_amount is not None:
        financial_lines.append(
            f"Valor do equipamento: {_format_brl(equipment_amount)}."
        )
    if (
        context.get("purchase_mode") == "both"
        and service_amount is not None
        and equipment_amount is not None
    ):
        financial_lines.append(
            f"Total: {_format_brl(service_amount + equipment_amount)}."
        )
    if service_kind == "diagnostics" and service_amount is not None:
        financial_lines.append(
            "O valor final será confirmado após o diagnóstico e pode variar "
            "se houver necessidade de peças, materiais ou um reparo mais complexo."
        )

    first_body = (
        " ".join(equipment_lines)
        if equipment_lines
        else f"Cotação para {service_label}."
    )
    service_body = "\n".join(financial_lines)

    updated = {
        **context,
        "quote_presented": True,
    }
    return _transition(
        ConversationState.QUOTE_DECISION,
        updated,
        _text_message(first_body),
        follow_ups=(
            _text_message(service_body),
            quote_decision_message(
                "Gostaria de já agendar a instalação?"
            ),
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
    service_kind = _service_kind(services, service_id)
    lines = [
        (
            f"Perfeito, {customer_name}.\n\nSó para confirmar:"
            if customer_name
            else "Perfeito.\n\nSó para confirmar:"
        ),
        f"• Serviço: {service_label}",
    ]
    if requirements.quantity > 1:
        lines.append(f"• Quantidade: {requirements.quantity}")
    lines.extend(
        (
            f"• Data: {date_short_label(selected_date)}",
            f"• Horário: {selected_time}",
        )
    )
    address = ServiceAddress.from_snapshot(context.get("service_address"))
    if address is not None:
        lines.append(f"• Endereço: {address.searchable_text}")
    model = _context_string(context, "equipment_model")
    if model:
        lines.append(f"• Equipamento: {model}")
    recommendation = context.get("recommended_equipment")
    if isinstance(recommendation, dict):
        label = recommendation.get("label")
        if isinstance(label, str) and label and not model:
            lines.append(f"• Equipamento sugerido: {label}")
    if context.get("work_at_height") is True:
        lines.append("• Detalhe: trabalho em altura (acima de 3 m)")
    property_type = _context_string(context, "property_type")
    property_labels = {
        "house": "Casa",
        "building": "Prédio",
        "condominium": "Condomínio",
    }
    if property_type in property_labels:
        lines.append(f"• Local: {property_labels[property_type]}")
    onsite_name = _context_string(context, "onsite_contact_name")
    if onsite_name:
        lines.append(f"• Pessoa no local: {onsite_name}")
    contact_phone = (
        _context_string(context, "contact_phone")
        or _context_string(context, "whatsapp_contact_phone")
    )
    if contact_phone:
        lines.append(f"• Contato: {contact_phone}")
    try:
        plan = await port.estimate(
            inbound.business_id,
            service_id,
            requirements,
        )
    except BookingRequiresHandoff:
        plan = None
    if plan is not None:
        service_price = plan.service.estimated_price
        equipment_price: Decimal | None = None
        if isinstance(recommendation, dict):
            raw_equipment_price = recommendation.get("price")
            if isinstance(raw_equipment_price, (int, float)) and not isinstance(
                raw_equipment_price, bool
            ):
                equipment_price = Decimal(str(raw_equipment_price))

        purchase_mode = _context_string(context, "purchase_mode")
        if purchase_mode == "both":
            if service_price is not None:
                service_label_price = (
                    "Valor base do serviço técnico"
                    if service_kind == "diagnostics"
                    else "Valor do serviço"
                )
                lines.append(
                    f"• {service_label_price}: {_format_brl(service_price)}"
                )
            if equipment_price is not None:
                lines.append(
                    f"• Valor do equipamento: {_format_brl(equipment_price)}"
                )
            if service_price is not None and equipment_price is not None:
                lines.append(
                    f"• Total: {_format_brl(service_price + equipment_price)}"
                )
        elif purchase_mode == "purchase":
            if equipment_price is not None:
                lines.append(
                    f"• Valor do equipamento: {_format_brl(equipment_price)}"
                )
        elif service_price is not None:
            price_label = (
                "Valor base do serviço técnico"
                if service_kind == "diagnostics"
                else (
                    "Valor do serviço"
                    if plan.service.pricing_type is PricingType.FIXED
                    else "Valor estimado do serviço"
                )
            )
            lines.append(f"• {price_label}: {_format_brl(service_price)}")

        if service_kind == "diagnostics" and service_price is not None:
            lines.append(
                "• O valor final será confirmado após o diagnóstico e pode variar "
                "se houver necessidade de peças, materiais ou um reparo mais complexo."
            )
        lines.append(
            "• Duração estimada: "
            f"{_format_duration(plan.service.estimated_duration_minutes)}"
        )
    lines.append("\nPosso confirmar?")
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
    if outbound.body:
        preserve_structured_confirmation = (
            outbound.interactive_id == "booking.confirmation"
            and len(outbound.body) <= 1024
        )
        chunks = (
            (outbound.body,)
            if preserve_structured_confirmation
            else _split_customer_message(outbound.body)
        )
        if len(chunks) > 1:
            if outbound.message_type == "text":
                outbound = replace(outbound, body=chunks[0])
                follow_ups = (
                    *(OutboundMessage(message_type="text", body=chunk) for chunk in chunks[1:]),
                    *follow_ups,
                )
            else:
                original = outbound
                outbound = OutboundMessage(message_type="text", body=chunks[0])
                follow_ups = (
                    *(OutboundMessage(message_type="text", body=chunk) for chunk in chunks[1:-1]),
                    replace(original, body=chunks[-1]),
                    *follow_ups,
                )
    return ConversationTransition(
        state=state,
        context=context,
        automation_enabled=automation_enabled,
        handoff_status=handoff_status,
        outbound=outbound,
        follow_ups=follow_ups,
    )


def _split_customer_message(body: str, max_chars: int = 180) -> tuple[str, ...]:
    normalized = " ".join(body.split())
    if len(normalized) <= max_chars and body.count("\n") < 4:
        return (body,)
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", normalized)
        if part.strip()
    ]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = sentence if not current else f"{current} {sentence}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return tuple(chunks or [normalized])


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

    key_tokens = {
        "split-installation": ("instal",),
        "cleaning": ("limpeza", "higien", "lavagem"),
        "preventive-maintenance": ("preventiv", "revis"),
        "diagnostics": ("diagnost", "corretiv", "manutencao corretiva"),
        "gas-recharge": ("recarga", "gas", "vazamento"),
    }
    if interpretation.service_key in key_tokens:
        tokens = key_tokens[interpretation.service_key]
        direct = [
            service
            for service in services
            if any(
                token in normalize_portuguese(service.label)
                for token in tokens
            )
        ]
        if len(direct) == 1:
            return direct[0]

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
    if requirements.site_allowed_start is not None:
        context["building_hours_start"] = requirements.site_allowed_start.strftime("%H:%M")
    if requirements.site_allowed_end is not None:
        context["site_allowed_end"] = requirements.site_allowed_end.strftime("%H:%M")
        context["building_hours_end"] = requirements.site_allowed_end.strftime("%H:%M")
        context["site_limit_answered"] = True
    for key, value in requirements.operational_details.items():
        if key in ALLOWED_CONTEXT_KEYS:
            context[key] = value
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
        for key, value in _clean_context(context).items()
        if key
        not in {
            "selected_date",
            "selected_time",
            "candidate_booking",
            "pending_customer_message",
            "pending_interactive_id",
        }
    }


def _requirements_from_context(context: dict[str, Any]) -> BookingRequirements:
    quantity = context.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        quantity = context.get("equipment_quantity")
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
    site_start = _context_time(context, "building_hours_start")
    site_limit = (
        _context_time(context, "building_hours_end")
        or _context_time(context, "site_allowed_end")
    )
    tubing_value = _context_string(context, "tubing_meters")
    tubing: Decimal | None = None
    if tubing_value is not None:
        try:
            tubing = Decimal(tubing_value)
        except Exception:
            tubing = None

    operational_keys = (
        "request_mode",
        "equipment_ownership",
        "equipment_model",
        "equipment_photo_received",
        "issue_video_required",
        "issue_video_received",
        "reported_issue",
        "equipment_quantity",
        "room_area_m2",
        "room_people_max",
        "equipment_preference",
        "equipment_cycle",
        "indoor_space_width_cm",
        "indoor_space_height_cm",
        "indoor_space_depth_cm",
        "indoor_space_unrestricted",
        "outdoor_space_width_cm",
        "outdoor_space_height_cm",
        "outdoor_space_depth_cm",
        "outdoor_space_unrestricted",
        "recommended_equipment",
        "delivery_method",
        "delivery_address",
        "installation_height_over_3m",
        "work_at_height",
        "property_type",
        "building_hours_start",
        "building_hours_end",
        "gate_instructions",
        "onsite_contact_mode",
        "onsite_contact_name",
        "contact_phone",
        "contact_phone_confirmed",
    )
    operational_details = {
        key: context[key]
        for key in operational_keys
        if key in context
    }
    recommendation = operational_details.get("recommended_equipment")
    if isinstance(recommendation, dict):
        selected_cycle = recommendation.get("selected_cycle")
        if selected_cycle in {"cold", "heat_cool"}:
            normalized_recommendation = dict(recommendation)
            normalized_recommendation["cycles"] = [selected_cycle]
            normalized_recommendation["selected_cycle"] = selected_cycle
            operational_details["recommended_equipment"] = normalized_recommendation
    if context.get("request_mode") == "quote":
        operational_details["quote_only"] = True
    return BookingRequirements(
        quantity=quantity,
        access_condition=access,
        address=address,
        site_allowed_start=site_start,
        site_allowed_end=site_limit,
        tubing_meters=tubing,
        operational_details=operational_details,
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

from __future__ import annotations

from dataclasses import dataclass

from app.conversations.constants import ConversationState
from app.conversations.interpreter import ConversationAct, ConversationIntent


@dataclass(frozen=True, slots=True)
class SemanticCase:
    family: str
    text: str
    intent: ConversationIntent | None
    act: ConversationAct | None = None
    forbidden_intents: tuple[ConversationIntent, ...] = ()
    forbidden_acts: tuple[ConversationAct, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextCase:
    family: str
    state: str
    text: str
    forbidden_context_keys: tuple[str, ...] = ()
    expected_state: str | None = None
    forbidden_calls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowCase:
    family: str
    state: str
    side_text: str
    followup_text: str
    context_items: tuple[tuple[str, object], ...] = ()
    side_expected_state: str | None = None
    side_contains: tuple[str, ...] = ()
    followup_expected_state: str | None = None
    followup_context_keys: tuple[str, ...] = ()
    required_calls: tuple[str, ...] = ()
    forbidden_calls: tuple[str, ...] = ()


# Corpus de regressão conhecido e aprovado. Discovery só vira regressão após passar
# por um oráculo determinístico e revisão do relatório.
REGRESSION_SEMANTIC = (
    SemanticCase("duration", "leva 2h?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "o serviço pode levar 3 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "a instalação dura 4 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("availability", "tem vaga pra essa semana?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "qnd vcs conseguem vir?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "qual dia dá pra atender?", ConversationIntent.AVAILABILITY),
    SemanticCase("reschedule", "quero trocar o horário", ConversationIntent.RESCHEDULE),
    SemanticCase("reschedule", "quero trocar o horaro", ConversationIntent.RESCHEDULE),
    SemanticCase("duration", "esse serviço demroa 2 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "esse serviço demorra 2 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "qnto tempo leva esse serviço?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "qto tempo leva esse serviço?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("availability", "tem horaro livre hoje?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "quando vcês conseguem vir?", ConversationIntent.AVAILABILITY),
)

REGRESSION_CONTEXT = (
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "esse aparelho é de 9 mil BTUs?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "são 3 aparelhos mesmo?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "posso parcelar em 10 vezes?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "demora 2 horas?", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "o modelo é de 12 mil BTUs", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "valeu viu", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "pera aí um minutinho", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "blz, valeu viu", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "valeu viu pfv", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "opa, pera ai um minutinho", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "rapidinho: pera ai um minutinho por favor", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_ADDRESS, "e aí, valeu viu", ("repair_attempts",), ConversationState.BOOKING_ADDRESS),
    ContextCase("social_pause", ConversationState.BOOKING_TIME, "amigo, pera aí um minutinho", ("repair_attempts",), ConversationState.BOOKING_TIME),
    ContextCase("social_pause", ConversationState.BOOKING_SERVICE, "moço, valeu", ("repair_attempts",), ConversationState.BOOKING_SERVICE),
    ContextCase("social_pause", ConversationState.BOOKING_CONFIRM, "seguinte, pera aí um minutinho", ("repair_attempts",), ConversationState.BOOKING_CONFIRM),
)

# Seeds de discovery. O runner gera variações adicionais de forma controlada.
DISCOVERY_SEEDS = (
    SemanticCase("duration", "esse serviço demora 2 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "vocês levam umas 2h para terminar?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "quanto tempo leva esse serviço?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("availability", "tem horário livre hoje?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "quando vocês conseguem vir?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "qual o próximo horário?", ConversationIntent.AVAILABILITY),
    SemanticCase("cancel_action", "quero cancelar meu agendamento", ConversationIntent.CANCEL),
    SemanticCase("cancel_question", "como funciona o cancelamento?", ConversationIntent.CANCEL_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("reschedule_action", "preciso remarcar meu agendamento", ConversationIntent.RESCHEDULE),
    SemanticCase("reschedule_question", "se eu precisar remarcar depois, posso?", ConversationIntent.RESCHEDULE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("price", "quanto custa esse serviço?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("price", "qual o valor da visita?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("negated_cancel", "não quero cancelar", None, ConversationAct.NEGATED_ACTION, (ConversationIntent.CANCEL, ConversationIntent.CANCEL_QUESTION)),
    SemanticCase("negated_reschedule", "não quero remarcar", None, ConversationAct.NEGATED_ACTION, (ConversationIntent.RESCHEDULE, ConversationIntent.RESCHEDULE_QUESTION)),
    SemanticCase("negated_handoff", "não quero falar com atendente", None, ConversationAct.NEGATED_ACTION, (ConversationIntent.HUMAN_HANDOFF,)),
    SemanticCase("correction_reschedule", "quero cancelar, na verdade quero remarcar", ConversationIntent.RESCHEDULE, ConversationAct.CORRECTION, (ConversationIntent.CANCEL,)),
    SemanticCase("correction_cancel", "quero remarcar, na verdade quero cancelar", ConversationIntent.CANCEL, ConversationAct.CORRECTION, (ConversationIntent.RESCHEDULE,)),
    SemanticCase("composed_duration", "são 2 aparelhos e demora 3 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("composed_price", "tenho 2 aparelhos, quanto custa o serviço?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("service_installation", "preciso instalar um ar condicionado", ConversationIntent.SERVICE_INTENT),
    SemanticCase("service_cleaning", "quero fazer uma limpeza no ar", ConversationIntent.SERVICE_INTENT),
    SemanticCase("service_diagnostics", "meu ar não está gelando", ConversationIntent.SERVICE_INTENT),
    SemanticCase("service_gas", "preciso de recarga de gás", ConversationIntent.SERVICE_INTENT),
    SemanticCase("service_pmoc", "vocês fazem PMOC?", ConversationIntent.SERVICE_INTENT, ConversationAct.SIDE_QUESTION),
    SemanticCase("service_question", "o que inclui esse serviço?", ConversationIntent.SERVICE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("technical_question", "qual a voltagem do aparelho?", ConversationIntent.TECHNICAL_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("equipment_purchase", "quero comprar um split", ConversationIntent.EQUIPMENT_PURCHASE),
    SemanticCase("human_handoff", "quero falar com uma pessoa", ConversationIntent.HUMAN_HANDOFF),
    SemanticCase("book_action", "quero agendar uma visita", ConversationIntent.BOOK),
    SemanticCase("informal_price", "fica quanto esse serviço?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("informal_availability", "tem algum horário livre?", ConversationIntent.AVAILABILITY),
)

_SOCIAL_PAUSE_CHECKPOINTS = (
    ConversationState.BOOKING_SERVICE,
    ConversationState.BOOKING_QUANTITY,
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
    ConversationState.BOOKING_DATE,
    ConversationState.BOOKING_TIME,
    ConversationState.BOOKING_ATTENDEE,
    ConversationState.BOOKING_ATTENDEE_NAME,
    ConversationState.BOOKING_PHONE_CONFIRM,
    ConversationState.BOOKING_CONFIRM,
    ConversationState.QUOTE_DECISION,
)


DISCOVERY_CONTEXT_SEEDS = (
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "essa visita tem garantia de 2 anos?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "tem 5 metros de tubulação?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "o endereço é número 9?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "meu aparelho é de 18 mil BTUs", ("selected_time", "candidate_booking"), None, ("confirm_dry_run",)),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "o serviço leva 3 horas?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "quanto custa? meu limite é 500 reais", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "a tubulação tem até 3 metros", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "no ambiente ficam 4 pessoas", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "meu endereço é número 12", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("date_guard", ConversationState.BOOKING_DATE, "são 3 aparelhos", ("selected_date",), ConversationState.BOOKING_DATE),
    ContextCase("date_guard", ConversationState.BOOKING_DATE, "tenho 5 metros de tubulação", ("selected_date",), ConversationState.BOOKING_DATE),
    ContextCase("date_guard", ConversationState.BOOKING_DATE, "o endereço é número 9", ("selected_date",), ConversationState.BOOKING_DATE),
    ContextCase("address_guard", ConversationState.BOOKING_ADDRESS, "são 2 aparelhos", ("service_address",), ConversationState.BOOKING_ADDRESS),
    ContextCase("address_guard", ConversationState.BOOKING_ADDRESS, "demora 3 horas?", ("service_address",), ConversationState.BOOKING_ADDRESS),
    ContextCase("address_guard", ConversationState.BOOKING_ADDRESS, "meu ar é de 12 mil BTUs", ("service_address",), ConversationState.BOOKING_ADDRESS),
    ContextCase("destructive_question_guard", ConversationState.BOOKING_CONFIRM, "como funciona o cancelamento?", (), ConversationState.BOOKING_CONFIRM, ("cancel_dry_run", "reschedule_dry_run")),
    ContextCase("destructive_question_guard", ConversationState.BOOKING_CONFIRM, "se eu precisar remarcar depois, posso?", (), ConversationState.BOOKING_CONFIRM, ("cancel_dry_run", "reschedule_dry_run")),
    ContextCase("negated_destructive_guard", ConversationState.BOOKING_CONFIRM, "não quero cancelar", (), ConversationState.BOOKING_CONFIRM, ("cancel_dry_run", "reschedule_dry_run")),
    ContextCase("negated_destructive_guard", ConversationState.BOOKING_CONFIRM, "não quero remarcar", (), ConversationState.BOOKING_CONFIRM, ("cancel_dry_run", "reschedule_dry_run")),
    ContextCase("correction_guard", ConversationState.BOOKING_TIME, "são 2 aparelhos, na verdade são 3", ("selected_time",), None, ("confirm_dry_run",)),
    ContextCase("time_number_roles", ConversationState.BOOKING_TIME, "são 2 aparelhos e o serviço leva 3 horas?", ("selected_time",), ConversationState.BOOKING_TIME, ("confirm_dry_run",)),
    ContextCase("quantity_number_roles", ConversationState.BOOKING_QUANTITY, "tenho 2 aparelhos e 5 metros de tubulação", (), None, ("confirm_dry_run",)),
) + tuple(
    ContextCase(
        "social_checkpoint",
        state,
        "valeu",
        ("repair_attempts",),
        state,
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    )
    for state in _SOCIAL_PAUSE_CHECKPOINTS
) + tuple(
    ContextCase(
        "pause_checkpoint",
        state,
        "pera aí um minutinho",
        ("repair_attempts",),
        state,
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    )
    for state in _SOCIAL_PAUSE_CHECKPOINTS
)

TYPO_REPLACEMENTS = {
    "vocês": ("vcs", "voces", "vcês"),
    "horário": ("horario", "horaro", "horáro"),
    "serviço": ("servico", "servisso", "serviço"),
    "demora": ("demroa", "demora", "demorra"),
    "instalação": ("instalacao", "instalaçao", "instalação"),
    "aparelho": ("aparelo", "aparelho", "apareio"),
    "tubulação": ("tubulacao", "tubulação", "tubulaçao"),
    "quanto": ("qnto", "quanto", "qto"),
}

PREFIXES = (
    "",
    "oi, ",
    "bom dia, ",
    "blz, ",
    "opa, ",
    "rapidinho: ",
    "e aí, ",
    "amigo, ",
    "moço, ",
    "seguinte, ",
)
SUFFIXES = ("", "?", " ??", " por favor", " pfv", " valeu", " blz")


# Cenários multi-turno: uma dúvida lateral deve ser respondida com os dados
# sintéticos da empresa e o fluxo deve retomar o checkpoint determinístico.
DISCOVERY_FLOW_CASES = (
    FlowCase(
        "resume_price_time",
        ConversationState.BOOKING_TIME,
        "quanto custa esse serviço?",
        "14:30",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("R$ 100,00",),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_duration_time",
        ConversationState.BOOKING_TIME,
        "quanto tempo leva esse serviço?",
        "09:00",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("30 minutos",),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_tubing_time",
        ConversationState.BOOKING_TIME,
        "quantos metros de tubulação estão incluídos?",
        "18:00",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("3 metros",),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_extra_tubing_price_time",
        ConversationState.BOOKING_TIME,
        "quanto custa cada metro adicional de tubulação?",
        "14:30",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("R$ 50,00", "metro"),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_cancel_question_time",
        ConversationState.BOOKING_TIME,
        "se eu precisar cancelar depois, posso?",
        "09:00",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("cancelar um agendamento futuro",),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("cancel_dry_run", "reschedule_dry_run", "confirm_dry_run"),
    ),
    FlowCase(
        "resume_reschedule_question_time",
        ConversationState.BOOKING_TIME,
        "se eu precisar remarcar depois, posso?",
        "18:00",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("selected_date", "2026-11-16"), ("quantity", 1)),
        ConversationState.BOOKING_TIME,
        ("remarcar um agendamento futuro",),
        ConversationState.BOOKING_ATTENDEE,
        ("selected_time",),
        (),
        ("cancel_dry_run", "reschedule_dry_run", "confirm_dry_run"),
    ),
    FlowCase(
        "resume_price_quantity",
        ConversationState.BOOKING_QUANTITY,
        "quanto custa esse serviço?",
        "2",
        (("service_id", "94000000-0000-0000-0000-000000000004"),),
        ConversationState.BOOKING_QUANTITY,
        ("R$ 100,00",),
        ConversationState.BOOKING_ADDRESS,
        ("quantity",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_duration_address",
        ConversationState.BOOKING_ADDRESS,
        "quanto tempo leva esse serviço?",
        "Rua das Flores, 123, São José dos Campos, SP",
        (("service_id", "94000000-0000-0000-0000-000000000004"), ("quantity", 1)),
        ConversationState.BOOKING_ADDRESS,
        ("30 minutos",),
        ConversationState.BOOKING_EQUIPMENT_MODEL,
        ("service_address",),
        (),
        ("confirm_dry_run", "cancel_dry_run", "reschedule_dry_run"),
    ),
    FlowCase(
        "resume_price_confirm",
        ConversationState.BOOKING_CONFIRM,
        "quanto custa esse serviço?",
        "pode confirmar",
        (
            ("service_id", "94000000-0000-0000-0000-000000000004"),
            ("selected_date", "2026-11-16"),
            ("selected_time", "14:30"),
            ("quantity", 1),
        ),
        ConversationState.BOOKING_CONFIRM,
        ("R$ 100,00",),
        ConversationState.POST_BOOKING_HELP,
        (),
        ("confirm_dry_run",),
        ("cancel_dry_run", "reschedule_dry_run"),
    ),
)

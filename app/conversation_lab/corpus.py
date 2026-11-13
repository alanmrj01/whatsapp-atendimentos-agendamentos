from __future__ import annotations

from dataclasses import dataclass

from app.conversations.constants import ConversationState
from app.conversations.interpreter import ConversationAct, ConversationIntent


@dataclass(frozen=True, slots=True)
class SemanticCase:
    family: str
    text: str
    intent: ConversationIntent
    act: ConversationAct | None = None


@dataclass(frozen=True, slots=True)
class ContextCase:
    family: str
    state: str
    text: str
    forbidden_context_keys: tuple[str, ...] = ()
    expected_state: str | None = None


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
)

# Seeds de discovery. O runner gera variações adicionais de forma controlada.
DISCOVERY_SEEDS = (
    SemanticCase("duration", "esse serviço demora 2 horas?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "vocês levam umas 2h para terminar?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("duration", "quanto tempo leva esse serviço?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
    SemanticCase("availability", "tem horário livre hoje?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "quando vocês conseguem vir?", ConversationIntent.AVAILABILITY),
    SemanticCase("availability", "qual o próximo horário?", ConversationIntent.AVAILABILITY),
)

DISCOVERY_CONTEXT_SEEDS = (
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "essa visita tem garantia de 2 anos?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "tem 5 metros de tubulação?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("time_guard", ConversationState.BOOKING_TIME, "o endereço é número 9?", ("selected_time",), ConversationState.BOOKING_TIME),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "quanto custa? meu limite é 500 reais", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "a tubulação tem até 3 metros", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
    ContextCase("quantity_guard", ConversationState.BOOKING_QUANTITY, "no ambiente ficam 4 pessoas", ("quantity", "equipment_quantity"), ConversationState.BOOKING_QUANTITY),
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

PREFIXES = ("", "oi, ", "bom dia, ", "blz, ", "opa, ", "rapidinho: ")
SUFFIXES = ("", "?", " ??", " por favor", " pfv")

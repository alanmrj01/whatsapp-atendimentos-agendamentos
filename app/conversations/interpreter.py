from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class ConversationIntent(StrEnum):
    GREETING = "greeting"
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    HUMAN_HANDOFF = "human_handoff"
    SERVICE_INTENT = "service_intent"
    EQUIPMENT_PURCHASE = "equipment_purchase"
    AVAILABILITY = "availability"
    PRICE_QUESTION = "price_question"
    DURATION_QUESTION = "duration_question"
    SERVICE_QUESTION = "service_question"
    CANCEL_QUESTION = "cancel_question"
    RESCHEDULE_QUESTION = "reschedule_question"
    UNKNOWN = "unknown"


class ConversationAct(StrEnum):
    SOCIAL = "social"
    CORRECTION = "correction"
    SIDE_QUESTION = "side_question"
    ADDITIONAL_REQUEST = "additional_request"
    NEGATED_ACTION = "negated_action"


@dataclass(frozen=True, slots=True)
class Interpretation:
    intent: ConversationIntent
    normalized_text: str
    service_key: str | None = None
    intents: frozenset[ConversationIntent] = frozenset()
    customer_name: str | None = None
    acts: frozenset[ConversationAct] = frozenset()
    equipment_budget_max: float | None = None
    service_budget_max: float | None = None
    total_budget_max: float | None = None

    def has(self, intent: ConversationIntent) -> bool:
        return intent in self.intents or self.intent is intent

    def has_act(self, act: ConversationAct) -> bool:
        return act in self.acts


SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "split-installation": (
        "instalar",
        "instalacao",
        "colocar um split",
        "instalacao de ar",
    ),
    "cleaning": ("limpar", "limpeza", "higienizar", "higienizacao", "lavar", "lavagem"),
    "preventive-maintenance": ("preventiva", "revisao", "revisar"),
    "diagnostics": (
        "nao gela",
        "nao esta gelando",
        "parou",
        "barulho",
        "pingando",
        "defeito",
        "manutencao",
        "corretiva",
        "diagnostico",
        "conserto",
        "arrumar",
    ),
    "gas-recharge": ("gas", "sem gas", "recarga", "vazamento"),
}

_GREETING_PHRASES = (
    "oi",
    "ola",
    "bom dia",
    "boa tarde",
    "boa noite",
    "tudo bem",
    "como vai",
)

_PRICE_PHRASES = (
    "quanto custa",
    "qual o valor",
    "qual valor",
    "preco",
    "valor do servico",
    "fica quanto",
    "cotacao",
    "orcamento",
    "pesquisa de preco",
    "mais barato",
    "mais em conta",
    "posso gastar",
    "so posso gastar",
    "ate r",
)

_DURATION_PHRASES = (
    "quanto tempo",
    "quanto demora",
    "demora quanto",
    "qual a duracao",
    "duracao",
    "leva quanto tempo",
)

_SERVICE_QUESTION_PHRASES = (
    "o que inclui",
    "esta incluso",
    "esta incluida",
    "inclui",
    "como funciona",
    "o que voces fazem",
    "o que e feito",
    "faz parte",
)

_SOCIAL_ONLY = frozenset(
    {
        "bom dia",
        "boa tarde",
        "boa noite",
        "ok",
        "okay",
        "entendi",
        "beleza",
        "obrigado",
        "obrigada",
        "show",
        "certo",
        "valeu",
        "perfeito",
        "tudo bem",
        "so uma duvida",
        "uma duvida",
    }
)

_CORRECTION_MARKERS = (
    "na verdade",
    "quis dizer",
    "corrigindo",
    "melhor dizendo",
)

_BARE_NAME_REJECT_PREFIXES = (
    "oi",
    "ola",
    "bom ",
    "boa ",
    "sim",
    "nao",
    "quero",
    "preciso",
    "gostaria",
    "agendar",
    "marcar",
    "limpar",
    "limpeza",
    "instalar",
    "instalacao",
    "manutencao",
    "cancelar",
    "remarcar",
    "quanto",
    "qual",
    "quando",
    "onde",
    "suave",
    "beleza",
    "tranquilo",
    "tudo bem",
    "ok",
    "certo",
)


class DeterministicConversationInterpreter:
    """Classificador determinístico multissinal para conversas em português."""

    def interpret(self, body: str | None) -> Interpretation:
        original = body or ""
        normalized = normalize_portuguese(original)
        if not normalized:
            return Interpretation(
                ConversationIntent.UNKNOWN,
                normalized,
                intents=frozenset({ConversationIntent.UNKNOWN}),
            )

        intents: set[ConversationIntent] = set()
        acts: set[ConversationAct] = set()
        service_key: str | None = None
        assertion = correction_focus(original)

        if normalized in _SOCIAL_ONLY:
            acts.add(ConversationAct.SOCIAL)
        if assertion != normalized or any(
            marker in normalized for marker in _CORRECTION_MARKERS
        ):
            acts.add(ConversationAct.CORRECTION)
        if re.search(r"\b(?:tambem|alem disso)\s+(?:quero|preciso|gostaria)\b", normalized):
            acts.add(ConversationAct.ADDITIONAL_REQUEST)
        if re.search(
            r"\bnao\s+(?:quero\s+|vou\s+|pretendo\s+|desejo\s+)?"
            r"(?:cancelar|cancelamento|desmarcar|remarcar|reagendar|"
            r"reagendamento|comprar|falar\s+com|(?:um\s+)?atendente)\b",
            normalized,
        ):
            acts.add(ConversationAct.NEGATED_ACTION)

        handoff_aliases = (
            "falar com atendente",
            "falar com alguem",
            "falar com uma pessoa",
            "quero um atendente",
            "atendente humano",
            "pessoa da equipe",
        )
        if _contains_any(assertion, handoff_aliases) and not any(
            _phrase_is_negated(normalized, alias) for alias in handoff_aliases
        ):
            intents.add(ConversationIntent.HUMAN_HANDOFF)
        reschedule_signal = _contains_any(
            assertion,
            (
                "remarcar",
                "reagendar",
                "reagendamento",
                "mudar horario",
                "trocar horario",
            ),
        )
        cancel_signal = _contains_any(
            assertion,
            ("cancelar", "cancelamento", "cancela", "desmarcar"),
        )
        reschedule_aliases = (
            "remarcar",
            "reagendar",
            "reagendamento",
            "mudar horario",
            "trocar horario",
        )
        if reschedule_signal and not any(
            _phrase_is_negated(assertion, alias) for alias in reschedule_aliases
        ):
            if _is_action_request(assertion, reschedule_aliases):
                intents.add(ConversationIntent.RESCHEDULE)
            else:
                intents.add(ConversationIntent.RESCHEDULE_QUESTION)
                acts.add(ConversationAct.SIDE_QUESTION)
        cancel_aliases = ("cancelar", "cancelamento", "cancela", "desmarcar")
        if cancel_signal and not any(
            _phrase_is_negated(assertion, alias) for alias in cancel_aliases
        ):
            if _is_action_request(assertion, cancel_aliases):
                intents.add(ConversationIntent.CANCEL)
            else:
                intents.add(ConversationIntent.CANCEL_QUESTION)
                acts.add(ConversationAct.SIDE_QUESTION)

        for candidate_key, aliases in SERVICE_ALIASES.items():
            if _contains_any(assertion, aliases):
                service_key = candidate_key
                intents.add(ConversationIntent.SERVICE_INTENT)
                break

        if _contains_equipment_purchase(assertion) and not _purchase_is_negated(normalized):
            intents.add(ConversationIntent.EQUIPMENT_PURCHASE)

        if _contains_any(normalized, ("tem horario", "disponibilidade", "quando pode", "qual horario")):
            intents.add(ConversationIntent.AVAILABILITY)
        if _contains_any(normalized, ("agendar", "marcar", "quero atendimento", "quero uma visita")):
            intents.add(ConversationIntent.BOOK)
        if _contains_any(normalized, _PRICE_PHRASES):
            intents.add(ConversationIntent.PRICE_QUESTION)
            acts.add(ConversationAct.SIDE_QUESTION)
        if _contains_any(normalized, _DURATION_PHRASES):
            intents.add(ConversationIntent.DURATION_QUESTION)
            acts.add(ConversationAct.SIDE_QUESTION)
        if _contains_any(normalized, _SERVICE_QUESTION_PHRASES):
            intents.add(ConversationIntent.SERVICE_QUESTION)
            acts.add(ConversationAct.SIDE_QUESTION)
        if _contains_greeting(normalized):
            intents.add(ConversationIntent.GREETING)
        if intents == {ConversationIntent.GREETING}:
            acts.add(ConversationAct.SOCIAL)

        customer_name = extract_customer_name(original, allow_bare=False)
        equipment_budget, service_budget, total_budget = extract_budget_constraints(
            original
        )
        if any(
            value is not None
            for value in (equipment_budget, service_budget, total_budget)
        ):
            intents.add(ConversationIntent.PRICE_QUESTION)
            acts.add(ConversationAct.SIDE_QUESTION)

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
            ConversationIntent.CANCEL_QUESTION,
            ConversationIntent.RESCHEDULE_QUESTION,
            ConversationIntent.GREETING,
        )
        primary = next(
            (intent for intent in precedence if intent in intents),
            ConversationIntent.UNKNOWN,
        )
        if primary is ConversationIntent.UNKNOWN:
            intents.add(ConversationIntent.UNKNOWN)
        return Interpretation(
            primary,
            normalized,
            service_key,
            frozenset(intents),
            customer_name,
            frozenset(acts),
            equipment_budget_max=equipment_budget,
            service_budget_max=service_budget,
            total_budget_max=total_budget,
        )


def extract_budget_constraints(
    value: str | None,
) -> tuple[float | None, float | None, float | None]:
    raw = " ".join((value or "").strip().split())
    if not raw:
        return None, None, None
    normalized = normalize_portuguese(raw)
    budget_markers = (
        "ate ",
        "no maximo",
        "orcamento",
        "posso gastar",
        "so posso gastar",
        "tenho r",
        "limite",
    )
    if not any(marker in normalized for marker in budget_markers):
        return None, None, None

    amount = _budget_amount(raw)
    if amount is None or amount <= 0:
        return None, None, None

    if any(
        marker in normalized
        for marker in (
            "orcamento total",
            "valor total",
            "com instalacao",
            "incluindo instalacao",
            "tudo incluso",
            "resolver tudo",
        )
    ):
        return None, None, amount

    equipment_terms = (
        "ar condicionado",
        "aparelho",
        "equipamento",
        "modelo",
        "btu",
        "wifi",
        "alexa",
        "inverter",
    )
    service_terms = (
        "servico",
        "instalacao",
        "manutencao",
        "limpeza",
        "recarga",
        "tecnico",
    )
    has_equipment = any(term in normalized for term in equipment_terms)
    has_service = any(term in normalized for term in service_terms)
    if has_service and not has_equipment:
        return None, amount, None
    return amount, None, None


def _budget_amount(value: str) -> float | None:
    patterns = (
        r"(?:até|ate|no\s+máximo|no\s+maximo|orçamento|orcamento|gastar|limite)"
        r"[^0-9]{0,30}(?:r\$\s*)?([0-9][0-9.,]*)\s*(mil)?",
        r"(?:r\$\s*)?([0-9][0-9.,]*)\s*(mil)?"
        r"[^a-zA-ZÀ-ÿ]{0,5}(?:de\s+)?(?:orçamento|orcamento|maximo|máximo)",
    )
    match = next(
        (
            candidate
            for pattern in patterns
            if (candidate := re.search(pattern, value, flags=re.IGNORECASE))
        ),
        None,
    )
    if match is None:
        return None
    token = match.group(1)
    thousands = bool(match.group(2))
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif token.count(".") >= 1:
        groups = token.split(".")
        if all(len(group) == 3 for group in groups[1:]):
            token = "".join(groups)
    try:
        amount = float(token)
    except ValueError:
        return None
    return amount * 1000 if thousands else amount


def correction_focus(value: str) -> str:
    """Return the most recent asserted clause, favoring explicit corrections."""

    normalized = normalize_portuguese(value)
    if not normalized:
        return normalized
    for marker in _CORRECTION_MARKERS:
        token = f" {marker} "
        if token in f" {normalized} ":
            return normalized.rsplit(marker, maxsplit=1)[-1].strip()

    raw_parts = re.split(r"[,;]", value)
    if len(raw_parts) > 1 and normalize_portuguese(raw_parts[-1]).startswith("nao "):
        head = normalize_portuguese(raw_parts[0])
        head = re.sub(r"^(?:e|eh)\s+", "", head)
        if head:
            return head
    if len(raw_parts) > 1 and normalize_portuguese(raw_parts[0]).startswith("nao "):
        tail = normalize_portuguese(raw_parts[-1])
        tail = re.sub(r"^(?:mas\s+)?(?:e\s+)?(?:quero\s+|prefiro\s+)?", "", tail)
        if tail:
            return tail
    return normalized


def _phrase_is_negated(value: str, phrase: str) -> bool:
    escaped = r"\s+".join(re.escape(token) for token in phrase.split())
    return bool(
        re.search(
            rf"\bnao(?:\s+(?:quero|vou|pretendo|desejo|preciso|e|eh))?\s+{escaped}\b",
            value,
        )
    )


def _purchase_is_negated(value: str) -> bool:
    return bool(
        re.search(
            r"\bnao\s+(?:quero\s+|vou\s+|pretendo\s+)?(?:comprar|compra|adquirir)\b",
            value,
        )
    )


def _is_action_request(value: str, aliases: tuple[str, ...]) -> bool:
    if any(_phrase_is_negated(value, alias) for alias in aliases):
        return False
    question_leads = (
        "como funciona",
        "posso",
        "pode",
        "e possivel",
        "depois posso",
        "se eu quiser",
        "quando posso",
    )
    if any(lead in value for lead in question_leads) and not any(
        token in value
        for token in ("quero", "preciso", "gostaria", "vou", "pode cancelar", "pode remarcar")
    ):
        return False
    return True


def normalize_portuguese(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


def extract_customer_name(
    value: str | None,
    *,
    allow_bare: bool,
) -> str | None:
    raw = " ".join((value or "").strip().split())
    if not raw:
        return None

    explicit_patterns = (
        r"\bmeu\s+nome\s+(?:e|é)\s+(.+?)(?=\s+e\s+(?:quero|preciso|gostaria|vim|estou)\b|[,;.!?]|$)",
        r"\bme\s+chamo\s+(.+?)(?=\s+e\s+(?:quero|preciso|gostaria|vim|estou)\b|[,;.!?]|$)",
        r"\bpode\s+me\s+chamar\s+de\s+(.+?)(?=\s+e\s+(?:quero|preciso|gostaria|vim|estou)\b|[,;.!?]|$)",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return _clean_name(match.group(1))

    if not allow_bare:
        return None
    normalized = normalize_portuguese(raw)
    if any(
        normalized == prefix.rstrip() or normalized.startswith(prefix)
        for prefix in _BARE_NAME_REJECT_PREFIXES
    ):
        return None
    if any(_contains_any(normalized, aliases) for aliases in SERVICE_ALIASES.values()):
        return None
    if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{0,59}", raw):
        return None
    words = raw.split()
    if not 1 <= len(words) <= 4:
        return None
    return _clean_name(raw)


def _clean_name(value: str) -> str | None:
    cleaned = " ".join(value.strip(" .,:;!?").split())
    if not cleaned or len(cleaned) > 60:
        return None
    if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{0,59}", cleaned):
        return None
    words = cleaned.split()
    if not 1 <= len(words) <= 4:
        return None
    if cleaned.isupper() or cleaned.islower():
        cleaned = cleaned.title()
    return cleaned


def _contains_equipment_purchase(value: str) -> bool:
    """Recognize purchase/quote intent for equipment without stealing install quotes."""

    tokens = value.split()
    has_equipment = (
        _contains_any(
            value,
            (
                "ar condicionado",
                "ar-condicionado",
                "split",
                "aparelho",
                "equipamento",
            ),
        )
        or any(
            token.startswith("condici") and token.endswith("onado")
            for token in tokens
        )
    )
    if not has_equipment:
        return False

    purchase_verbs = (
        "comprar",
        "compra",
        "adquirir",
        "vender",
        "vendem",
        "vende",
    )
    if _contains_any(value, purchase_verbs):
        return True

    installation_phrases = (
        "instalacao",
        "instalar",
        "colocar um split",
        "colocar o ar",
    )
    direct_need = re.search(
        r"\b(?:preciso|quero|gostaria)\s+(?:de\s+)?(?:um|uma)\s+"
        r"(?:ar\s+condicionado|split|aparelho|equipamento)\b",
        value,
    )
    if direct_need and not _contains_any(value, installation_phrases):
        return True

    quote_phrases = (
        "cotacao",
        "orcamento",
        "cotar",
        "preco do aparelho",
        "preco do ar condicionado",
        "valor do aparelho",
        "valor do ar condicionado",
    )
    if not _contains_any(value, quote_phrases):
        return False

    return not _contains_any(value, installation_phrases)


def _contains_greeting(value: str) -> bool:
    padded = f" {value} "
    return any(
        padded.startswith(f" {phrase} ")
        or f" {phrase} " in padded
        for phrase in _GREETING_PHRASES
    )


def _contains_any(value: str, aliases: tuple[str, ...]) -> bool:
    padded = f" {value} "
    return any(f" {alias} " in padded for alias in aliases)

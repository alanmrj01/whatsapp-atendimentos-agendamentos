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
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Interpretation:
    intent: ConversationIntent
    normalized_text: str
    service_key: str | None = None
    intents: frozenset[ConversationIntent] = frozenset()
    customer_name: str | None = None

    def has(self, intent: ConversationIntent) -> bool:
        return intent in self.intents or self.intent is intent


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
        service_key: str | None = None

        if _contains_any(
            normalized,
            ("falar com atendente", "falar com alguem", "atendente", "humano", "pessoa"),
        ):
            intents.add(ConversationIntent.HUMAN_HANDOFF)
        if _contains_any(normalized, ("remarcar", "reagendar", "mudar horario", "trocar horario")):
            intents.add(ConversationIntent.RESCHEDULE)
        if _contains_any(normalized, ("cancelar", "cancela", "desmarcar")):
            intents.add(ConversationIntent.CANCEL)

        for candidate_key, aliases in SERVICE_ALIASES.items():
            if _contains_any(normalized, aliases):
                service_key = candidate_key
                intents.add(ConversationIntent.SERVICE_INTENT)
                break

        if _contains_equipment_purchase(normalized):
            intents.add(ConversationIntent.EQUIPMENT_PURCHASE)

        if _contains_any(normalized, ("tem horario", "disponibilidade", "quando pode", "qual horario")):
            intents.add(ConversationIntent.AVAILABILITY)
        if _contains_any(normalized, ("agendar", "marcar", "quero atendimento", "quero uma visita")):
            intents.add(ConversationIntent.BOOK)
        if _contains_any(normalized, _PRICE_PHRASES):
            intents.add(ConversationIntent.PRICE_QUESTION)
        if _contains_any(normalized, _DURATION_PHRASES):
            intents.add(ConversationIntent.DURATION_QUESTION)
        if _contains_any(normalized, _SERVICE_QUESTION_PHRASES):
            intents.add(ConversationIntent.SERVICE_QUESTION)
        if _contains_greeting(normalized):
            intents.add(ConversationIntent.GREETING)

        customer_name = extract_customer_name(original, allow_bare=False)

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
        )


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
    purchase_verbs = (
        "comprar",
        "compra",
        "adquirir",
        "vender",
        "vendem",
        "vende",
    )
    if not _contains_any(value, purchase_verbs):
        return False

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
    return has_equipment


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

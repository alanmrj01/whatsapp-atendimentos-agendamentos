from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum


class ConversationIntent(StrEnum):
    GREETING = "greeting"
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    HUMAN_HANDOFF = "human_handoff"
    SERVICE_INTENT = "service_intent"
    AVAILABILITY = "availability"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Interpretation:
    intent: ConversationIntent
    normalized_text: str
    service_key: str | None = None


SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "split-installation": (
        "instalar",
        "instalacao",
        "colocar um split",
        "instalar ar condicionado",
        "instalar ar",
        "instalar aparelho",
        "colocar ar condicionado",
        "montar ar condicionado",
        "instalacao de split",
        "instalacao do ar",
        "instalacao de ar",
        "tenho um ar para instalar",
        "comprei um ar e quero instalar",
    ),
    "cleaning": (
        "limpar",
        "limpeza",
        "higienizar",
        "higienizacao",
        "lavar o ar",
        "lavagem do ar",
        "limpar meu ar condicionado",
        "limpeza do split",
        "higienizar o split",
        "ar esta sujo",
        "split esta sujo",
        "limpeza de ar condicionado",
        "higienizacao de ar condicionado",
    ),
    "preventive-maintenance": (
        "preventiva",
        "manutencao preventiva",
        "revisao",
        "revisar",
        "fazer uma revisao",
        "revisar o ar",
        "revisao do ar condicionado",
    ),
    "diagnostics": (
        "nao gela",
        "nao esta gelando",
        "nao esta refrigerando",
        "parou",
        "parou de funcionar",
        "barulho",
        "fazendo barulho",
        "pingando",
        "vazando agua",
        "defeito",
        "manutencao",
        "corretiva",
        "diagnostico",
        "ar com problema",
        "ar nao funciona",
        "ar nao liga",
    ),
    "gas-recharge": (
        "gas",
        "sem gas",
        "recarga",
        "recarga de gas",
        "colocar gas",
        "completar gas",
        "vazamento",
        "teste de vazamento",
        "acho que esta sem gas",
    ),
}

_STOPWORDS = frozenset(
    {
        "a",
        "ao",
        "as",
        "com",
        "da",
        "de",
        "do",
        "e",
        "em",
        "eu",
        "fazer",
        "gostaria",
        "meu",
        "minha",
        "no",
        "o",
        "os",
        "para",
        "por",
        "preciso",
        "quero",
        "um",
        "uma",
        "voces",
        "voce",
    }
)

_SEMANTIC_GROUPS = {
    "limpeza": {
        "limpar",
        "limpeza",
        "higienizar",
        "higienizacao",
        "lavar",
        "lavagem",
        "sujeira",
        "sujo",
    },
    "instalacao": {
        "instalar",
        "instalacao",
        "colocar",
        "montar",
        "fixar",
    },
    "manutencao": {
        "manutencao",
        "revisao",
        "revisar",
        "preventiva",
        "corretiva",
        "diagnostico",
        "defeito",
        "problema",
    },
    "gas": {
        "gas",
        "recarga",
        "vazamento",
        "pressao",
    },
    "ar_condicionado": {
        "ar",
        "condicionado",
        "split",
        "aparelho",
        "evaporadora",
        "condensadora",
    },
}

_CANONICAL_TOKEN: dict[str, str] = {}
for canonical, variants in _SEMANTIC_GROUPS.items():
    for variant in variants:
        _CANONICAL_TOKEN[variant] = canonical


class DeterministicConversationInterpreter:
    """Portuguese intent classification with contextual deterministic semantics."""

    def interpret(self, body: str | None) -> Interpretation:
        normalized = normalize_portuguese(body or "")
        if not normalized:
            return Interpretation(ConversationIntent.UNKNOWN, normalized)

        if _contains_any(
            normalized,
            (
                "falar com atendente",
                "falar com alguem",
                "falar com uma pessoa",
                "atendente",
                "humano",
                "pessoa da equipe",
            ),
        ):
            return Interpretation(ConversationIntent.HUMAN_HANDOFF, normalized)
        if _contains_any(
            normalized,
            (
                "remarcar",
                "reagendar",
                "mudar horario",
                "trocar horario",
                "mudar meu agendamento",
            ),
        ):
            return Interpretation(ConversationIntent.RESCHEDULE, normalized)
        if _contains_any(
            normalized,
            ("cancelar", "cancela", "desmarcar", "cancelar agendamento"),
        ):
            return Interpretation(ConversationIntent.CANCEL, normalized)

        for service_key, aliases in SERVICE_ALIASES.items():
            if _contains_any(normalized, aliases):
                return Interpretation(
                    ConversationIntent.SERVICE_INTENT,
                    normalized,
                    service_key,
                )

        if _contains_any(
            normalized,
            (
                "tem horario",
                "tem vaga",
                "disponibilidade",
                "quando pode",
                "quando conseguem",
                "qual horario",
                "horario disponivel",
            ),
        ):
            return Interpretation(ConversationIntent.AVAILABILITY, normalized)
        if _contains_any(
            normalized,
            (
                "agendar",
                "marcar",
                "agendamento",
                "quero atendimento",
                "quero uma visita",
                "pode marcar",
                "quero reservar um horario",
            ),
        ):
            return Interpretation(ConversationIntent.BOOK, normalized)
        if normalized in {
            "oi",
            "ola",
            "bom dia",
            "boa tarde",
            "boa noite",
            "tudo bem",
            "opa",
        }:
            return Interpretation(ConversationIntent.GREETING, normalized)
        return Interpretation(ConversationIntent.UNKNOWN, normalized)


def normalize_portuguese(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


def service_match_score(
    message: str,
    label: str,
    examples: tuple[str, ...] | list[str] = (),
) -> float:
    """Return a deterministic semantic score in the [0, 1] range.

    It intentionally combines phrase containment, canonical HVAC concepts,
    token overlap and conservative fuzzy similarity. This keeps matching
    auditable while handling wording variations beyond exact aliases.
    """

    normalized_message = normalize_portuguese(message)
    if not normalized_message:
        return 0.0
    candidates = (label, *examples)
    return max(
        (_phrase_similarity(normalized_message, normalize_portuguese(candidate))
         for candidate in candidates
         if normalize_portuguese(candidate)),
        default=0.0,
    )


def _phrase_similarity(message: str, candidate: str) -> float:
    if not candidate:
        return 0.0
    if candidate in message:
        return 0.98 if len(candidate) >= 5 else 0.78
    if message in candidate and len(message) >= 5:
        return 0.92

    message_tokens = _semantic_tokens(message)
    candidate_tokens = _semantic_tokens(candidate)
    if not message_tokens or not candidate_tokens:
        return SequenceMatcher(None, message, candidate).ratio() * 0.45

    intersection = len(message_tokens & candidate_tokens)
    union = len(message_tokens | candidate_tokens)
    jaccard = intersection / union if union else 0.0
    coverage = intersection / len(candidate_tokens)
    fuzzy = SequenceMatcher(None, message, candidate).ratio()

    score = (0.5 * coverage) + (0.3 * jaccard) + (0.2 * fuzzy)
    if intersection >= 2:
        score += 0.08
    return min(score, 1.0)


def _semantic_tokens(value: str) -> set[str]:
    tokens = {
        token
        for token in normalize_portuguese(value).split()
        if token not in _STOPWORDS and len(token) > 1
    }
    return {_CANONICAL_TOKEN.get(token, token) for token in tokens}


def _contains_any(value: str, aliases: tuple[str, ...]) -> bool:
    padded = f" {value} "
    return any(f" {alias} " in padded for alias in aliases)

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
        "instalacao de ar",
    ),
    "cleaning": ("limpar", "limpeza", "higienizar", "higienizacao"),
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
    ),
    "gas-recharge": ("gas", "sem gas", "recarga", "vazamento"),
}


class DeterministicConversationInterpreter:
    """Portuguese intent classification without external AI or customer data."""

    def interpret(self, body: str | None) -> Interpretation:
        normalized = normalize_portuguese(body or "")
        if not normalized:
            return Interpretation(ConversationIntent.UNKNOWN, normalized)

        if _contains_any(
            normalized,
            ("falar com atendente", "falar com alguem", "atendente", "humano", "pessoa"),
        ):
            return Interpretation(ConversationIntent.HUMAN_HANDOFF, normalized)
        if _contains_any(normalized, ("remarcar", "reagendar", "mudar horario", "trocar horario")):
            return Interpretation(ConversationIntent.RESCHEDULE, normalized)
        if _contains_any(normalized, ("cancelar", "cancela", "desmarcar")):
            return Interpretation(ConversationIntent.CANCEL, normalized)

        for service_key, aliases in SERVICE_ALIASES.items():
            if _contains_any(normalized, aliases):
                return Interpretation(
                    ConversationIntent.SERVICE_INTENT,
                    normalized,
                    service_key,
                )

        if _contains_any(normalized, ("tem horario", "disponibilidade", "quando pode", "qual horario")):
            return Interpretation(ConversationIntent.AVAILABILITY, normalized)
        if _contains_any(normalized, ("agendar", "marcar", "quero atendimento", "quero uma visita")):
            return Interpretation(ConversationIntent.BOOK, normalized)
        if normalized in {
            "oi",
            "ola",
            "bom dia",
            "boa tarde",
            "boa noite",
            "tudo bem",
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


def _contains_any(value: str, aliases: tuple[str, ...]) -> bool:
    padded = f" {value} "
    return any(f" {alias} " in padded for alias in aliases)

from __future__ import annotations

import re
from enum import StrEnum

HUMAN_CONTROL_WINDOW_PRESETS = (
    5,
    10,
    20,
    30,
    60,
    120,
    240,
    360,
    720,
    1440,
    2160,
)
DEFAULT_HUMAN_CONTROL_WINDOW_MINUTES = 2160
DEFAULT_ASSISTANT_GREETING = "Olá! Como posso ajudar com seu ar-condicionado?"
DEFAULT_ASSISTANT_FALLBACK = "Desculpa, não entendi. como posso te ajudar?"
DEFAULT_ASSISTANT_HANDOFF = (
    "Seu atendimento foi encaminhado para uma pessoa da equipe."
)
ASSISTANT_MESSAGE_MAX_LENGTH = 1000
_INDIVIDUAL_ID = re.compile(r"^[1-9][0-9]{6,14}$")


class ExclusionMode(StrEnum):
    IGNORE = "ignore"
    HUMAN_ONLY = "human_only"


class AutomationDecision(StrEnum):
    ALLOWED = "allowed"
    DISABLED = "automation_disabled"
    IGNORED = "ignored"
    HUMAN_ONLY = "human_only"
    TEMPORARILY_SUPPRESSED = "temporarily_suppressed"


def validate_human_control_window(value: int) -> int:
    if isinstance(value, bool) or value not in HUMAN_CONTROL_WINDOW_PRESETS:
        raise ValueError("Human control window must use an allowed preset")
    return value


def normalize_whatsapp_id(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    if _INDIVIDUAL_ID.fullmatch(normalized) is None:
        raise ValueError("WhatsApp identifier must represent one individual")
    return normalized


def _looks_like_brazil_national_number(value: str) -> bool:
    if len(value) not in {10, 11} or not value.isdigit():
        return False
    try:
        area_code = int(value[:2])
    except ValueError:
        return False
    if not 11 <= area_code <= 99:
        return False
    subscriber_prefix = value[2]
    if len(value) == 11:
        return subscriber_prefix == "9"
    return subscriber_prefix in {"2", "3", "4", "5"}


def whatsapp_id_lookup_aliases(value: str) -> tuple[str, ...]:
    """Return canonical and legacy aliases for one individual WhatsApp identity.

    Brazilian device contacts are commonly exposed without country code. Runtime
    lookups must still match those historical exclusions against Meta's E.164-like
    WhatsApp ID, while leaving non-Brazilian international IDs untouched.
    """

    normalized = normalize_whatsapp_id(value)
    candidates: list[str] = []

    if normalized.startswith("55") and _looks_like_brazil_national_number(
        normalized[2:]
    ):
        candidates.extend((normalized, normalized[2:]))
    elif _looks_like_brazil_national_number(normalized):
        candidates.extend(("55" + normalized, normalized))
    else:
        candidates.append(normalized)

    return tuple(dict.fromkeys(candidates))


def normalize_automation_exclusion_whatsapp_id(value: str) -> str:
    """Canonicalize exclusions so future Brazilian contacts persist with +55."""

    return whatsapp_id_lookup_aliases(value)[0]


def normalize_assistant_message(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > ASSISTANT_MESSAGE_MAX_LENGTH:
        raise ValueError("Assistant message has an invalid length")
    if "<" in normalized or ">" in normalized:
        raise ValueError("Assistant message must be plain text")
    return normalized

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
_INDIVIDUAL_ID = re.compile(r"^[1-9][0-9]{6,14}$")


class ExclusionMode(StrEnum):
    IGNORE = "ignore"
    HUMAN_ONLY = "human_only"


class AutomationDecision(StrEnum):
    ALLOWED = "allowed"
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

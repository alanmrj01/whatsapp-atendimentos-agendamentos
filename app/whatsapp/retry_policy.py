from __future__ import annotations

OUTBOUND_RETRY_ATTEMPT_KEY = "_alovia_retry_attempt"
OUTBOUND_RETRY_DELAY_SECONDS = 120.0
MAX_OUTBOUND_RETRY_ATTEMPTS = 3

RETRYABLE_OUTBOUND_MESSAGE_TYPES = {
    "text",
    "image",
    "interactive_button",
    "interactive_list",
    "template",
}


def current_outbound_retry_attempt(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    value = payload.get(OUTBOUND_RETRY_ATTEMPT_KEY)
    return value if isinstance(value, int) and value >= 0 else 0

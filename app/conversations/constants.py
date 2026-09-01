from enum import StrEnum


class ConversationState(StrEnum):
    START = "START"
    MENU = "MENU"
    BOOKING_SERVICE = "BOOKING_SERVICE"
    BOOKING_DATE = "BOOKING_DATE"
    BOOKING_TIME = "BOOKING_TIME"
    BOOKING_CONFIRM = "BOOKING_CONFIRM"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    COMPLETED = "COMPLETED"


MENU_BOOK = "menu.book"
MENU_RESCHEDULE = "menu.reschedule"
MENU_CANCEL = "menu.cancel"
MENU_HUMAN = "menu.human"

BOOKING_CONFIRM = "booking.confirm"
BOOKING_BACK = "booking.back"
BOOKING_CANCEL = "booking.cancel"

ALLOWED_CONTEXT_KEYS = frozenset(
    {
        "service_id",
        "selected_date",
        "selected_time",
        "candidate_booking",
    }
)

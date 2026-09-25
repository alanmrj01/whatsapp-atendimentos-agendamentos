from enum import StrEnum


class ConversationState(StrEnum):
    START = "START"
    CUSTOMER_NAME = "CUSTOMER_NAME"
    MENU = "MENU"
    BOOKING_SERVICE = "BOOKING_SERVICE"
    BOOKING_QUANTITY = "BOOKING_QUANTITY"
    BOOKING_ACCESS = "BOOKING_ACCESS"
    BOOKING_ADDRESS = "BOOKING_ADDRESS"
    BOOKING_TUBING = "BOOKING_TUBING"
    BOOKING_SITE_LIMIT = "BOOKING_SITE_LIMIT"
    BOOKING_WEEKDAY = "BOOKING_WEEKDAY"
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
TUBING_CONFIRM = "tubing.confirm"
TUBING_UNKNOWN = "tubing.unknown"
ADDRESS_CITY_CONFIRM = "address.city.confirm"
ADDRESS_CITY_OTHER = "address.city.other"
RESCHEDULE_CONFIRM = "reschedule.confirm"
CANCEL_CONFIRM = "cancel.confirm"
CANCEL_ABORT = "cancel.abort"

ACCESS_NORMAL = "access.normal"
ACCESS_DIFFICULT = "access.difficult"
ACCESS_UNKNOWN = "access.unknown"

SITE_LIMIT_NONE = "site_limit.none"
SITE_LIMIT_17 = "site_limit.17:00"
SITE_LIMIT_18 = "site_limit.18:00"

QUANTITY_OPTION_LIMIT = 5

ALLOWED_CONTEXT_KEYS = frozenset(
    {
        "appointment_id",
        "service_id",
        "selected_date",
        "selected_time",
        "candidate_booking",
        "quantity",
        "access_condition",
        "service_address",
        "pending_service_address",
        "pending_address_city_guess",
        "awaiting_address_city",
        "tubing_meters",
        "pending_tubing_meters",
        "tubing_length_answered",
        "site_allowed_end",
        "site_limit_answered",
        "pending_customer_message",
        "pending_interactive_id",
    }
)

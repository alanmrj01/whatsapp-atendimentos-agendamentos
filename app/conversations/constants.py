from enum import StrEnum


class ConversationState(StrEnum):
    START = "START"
    CUSTOMER_NAME = "CUSTOMER_NAME"
    MENU = "MENU"
    BOOKING_SERVICE = "BOOKING_SERVICE"
    BOOKING_QUANTITY = "BOOKING_QUANTITY"
    BOOKING_ACCESS = "BOOKING_ACCESS"
    BOOKING_ADDRESS = "BOOKING_ADDRESS"
    BOOKING_EQUIPMENT_OWNERSHIP = "BOOKING_EQUIPMENT_OWNERSHIP"
    BOOKING_EQUIPMENT_MODEL = "BOOKING_EQUIPMENT_MODEL"
    BOOKING_EQUIPMENT_PROFILE = "BOOKING_EQUIPMENT_PROFILE"
    BOOKING_INSTALLATION_HEIGHT = "BOOKING_INSTALLATION_HEIGHT"
    BOOKING_PROPERTY = "BOOKING_PROPERTY"
    BOOKING_BUILDING_HOURS = "BOOKING_BUILDING_HOURS"
    BOOKING_GATE_DETAILS = "BOOKING_GATE_DETAILS"
    BOOKING_TUBING = "BOOKING_TUBING"
    BOOKING_SITE_LIMIT = "BOOKING_SITE_LIMIT"
    BOOKING_WEEKDAY = "BOOKING_WEEKDAY"
    BOOKING_DATE = "BOOKING_DATE"
    BOOKING_TIME = "BOOKING_TIME"
    BOOKING_ATTENDEE = "BOOKING_ATTENDEE"
    BOOKING_ATTENDEE_NAME = "BOOKING_ATTENDEE_NAME"
    BOOKING_PHONE_CONFIRM = "BOOKING_PHONE_CONFIRM"
    BOOKING_CONFIRM = "BOOKING_CONFIRM"
    QUOTE_DECISION = "QUOTE_DECISION"
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
EQUIPMENT_INSTALLATION = "equipment.installation"
EQUIPMENT_PURCHASE = "equipment.purchase"
EQUIPMENT_BOTH = "equipment.both"
EQUIPMENT_HAS = "equipment.has"
EQUIPMENT_NEEDS = "equipment.needs"
EQUIPMENT_MODEL_KNOWN = "equipment.model.known"
EQUIPMENT_MODEL_RECOMMEND = "equipment.model.recommend"
EQUIPMENT_PREF_MODERN = "equipment.preference.modern"
EQUIPMENT_PREF_COST_BENEFIT = "equipment.preference.cost_benefit"
EQUIPMENT_PREF_ECONOMY = "equipment.preference.economy"
HEIGHT_AT_MOST_3M = "height.at_most_3m"
HEIGHT_OVER_3M = "height.over_3m"
PROPERTY_HOUSE = "property.house"
PROPERTY_BUILDING = "property.building"
PROPERTY_CONDOMINIUM = "property.condominium"
ATTENDEE_CUSTOMER = "attendee.customer"
ATTENDEE_OTHER = "attendee.other"
PHONE_CONFIRM = "phone.confirm"
PHONE_OTHER = "phone.other"
QUOTE_SCHEDULE = "quote.schedule"
QUOTE_FINISH = "quote.finish"
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
        "service_clarification",
        "request_mode",
        "equipment_ownership",
        "equipment_model_known",
        "equipment_model",
        "equipment_quantity",
        "room_area_m2",
        "room_people_max",
        "equipment_preference",
        "equipment_profile_started_at",
        "equipment_profile_last_answer_at",
        "recommended_equipment",
        "installation_height_over_3m",
        "work_at_height",
        "tube_disclaimer_sent",
        "property_type",
        "building_hours_start",
        "building_hours_end",
        "gate_instructions",
        "onsite_contact_mode",
        "onsite_contact_name",
        "whatsapp_contact_phone",
        "contact_phone",
        "contact_phone_confirmed",
        "quote_presented",
        "purchase_only",
        "awaiting_other_phone",
        "repair_attempts",
        "tubing_meters",
        "pending_tubing_meters",
        "tubing_length_answered",
        "site_allowed_end",
        "site_limit_answered",
        "pending_customer_message",
        "pending_interactive_id",
    }
)

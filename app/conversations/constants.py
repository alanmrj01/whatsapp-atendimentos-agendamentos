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
    BOOKING_EQUIPMENT_DELIVERY = "BOOKING_EQUIPMENT_DELIVERY"
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
    POST_BOOKING_HELP = "POST_BOOKING_HELP"
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
POST_BOOKING_HELP_YES = "post_booking.help.yes"
POST_BOOKING_HELP_NO = "post_booking.help.no"
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
EQUIPMENT_CYCLE_COLD = "equipment.cycle.cold"
EQUIPMENT_CYCLE_HEAT_COOL = "equipment.cycle.heat_cool"
EQUIPMENT_SPACE_NO_LIMIT = "equipment.space.no_limit"
EQUIPMENT_DELIVERY_PICKUP = "equipment.delivery.pickup"
EQUIPMENT_DELIVERY_ADDRESS = "equipment.delivery.address"
EQUIPMENT_DELIVERY_WITH_INSTALLATION = "equipment.delivery.with_installation"
EQUIPMENT_INSTALLATION_SAME_ADDRESS = "equipment.installation.same_address"
EQUIPMENT_INSTALLATION_OTHER_ADDRESS = "equipment.installation.other_address"
CHANGE_CONFIRM = "change.confirm"
CHANGE_KEEP = "change.keep"
MEDIA_HANDOFF = "media.handoff"
MEDIA_CONTINUE_TEXT = "media.continue_text"
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
        "equipment_photo_requested",
        "equipment_photo_received",
        "issue_video_required",
        "issue_video_requested",
        "issue_video_received",
        "reported_issue",
        "equipment_quantity",
        "equipment_profile_intro_sent",
        "room_area_m2",
        "room_people_max",
        "equipment_preference",
        "equipment_cycle",
        "indoor_space_width_cm",
        "indoor_space_height_cm",
        "indoor_space_depth_cm",
        "indoor_space_unrestricted",
        "outdoor_space_width_cm",
        "outdoor_space_height_cm",
        "outdoor_space_depth_cm",
        "outdoor_space_unrestricted",
        "recommended_equipment",
        "equipment_suggestion",
        "recommendation_presented",
        "delivery_method",
        "fulfillment_type",
        "pickup_address",
        "delivery_fee_per_km",
        "equipment_budget_max",
        "service_budget_max",
        "total_budget_max",
        "delivery_address",
        "address_purpose",
        "delivery_installation_match_pending",
        "pending_change_action",
        "pending_change_label",
        "pending_service_change_id",
        "pending_service_change_label",
        "media_handoff_pending",
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
        "quote_paused",
        "fallback_variant",
        "catalog_mismatch_attempts",
        "catalog_mismatch_query",
        "purchase_mode",
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

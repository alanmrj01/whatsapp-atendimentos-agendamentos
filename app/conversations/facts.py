from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.conversations.interpreter import normalize_portuguese

_BRANDS = (
    "lg",
    "samsung",
    "midea",
    "springer",
    "gree",
    "daikin",
    "elgin",
    "electrolux",
    "philco",
    "tcl",
    "hisense",
    "fujitsu",
    "carrier",
    "hitachi",
    "consul",
)

_QUOTE_PHRASES = (
    "cotacao",
    "orcamento",
    "pesquisa de preco",
    "pesquisa de valor",
    "so pesquisando preco",
    "apenas pesquisando preco",
)

_MODERN_PHRASES = (
    "mais moderno",
    "moderno",
    "ultima geracao",
    "mais tecnologia",
    "tecnologico",
    "wifi",
    "inteligente",
    "premium",
)
_COST_BENEFIT_PHRASES = (
    "custo beneficio",
    "custo-beneficio",
    "equilibrado",
    "bom custo",
    "bom e barato",
)
_ECONOMY_PHRASES = (
    "mais barato",
    "mais em conta",
    "maximo de economia",
    "economizar o maximo",
    "menor preco",
    "economia financeira",
    "mais economico no preco",
)

_SELF_CONTACT_PHRASES = (
    "eu vou estar",
    "eu estarei",
    "serei eu",
    "sou eu que vou",
    "eu mesmo",
    "eu mesma",
)
_OTHER_CONTACT_PHRASES = (
    "outra pessoa",
    "outra pessoa vai",
    "nao vou estar",
    "não vou estar",
    "minha esposa",
    "meu marido",
    "meu pai",
    "minha mae",
    "minha mãe",
    "meu filho",
    "minha filha",
)

_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?55\s*)?(\d{2})\s*(\d{4,5})[-\s]?(\d{4})(?!\d)")


def enrich_context_from_message(
    context: dict[str, Any],
    body: str | None,
    *,
    whatsapp_id: str | None = None,
) -> dict[str, Any]:
    """Extract reusable customer facts independently of the current FSM state.

    The extractor is intentionally conservative: it stores only facts with clear
    lexical evidence. State-specific handlers may still interpret short answers
    such as "sim"/"não" using the question currently being asked.
    """

    raw = " ".join((body or "").strip().split())
    if not raw:
        return dict(context)

    normalized = normalize_portuguese(raw)
    updated = dict(context)

    if any(phrase in normalized for phrase in _QUOTE_PHRASES):
        updated["request_mode"] = "quote"

    ownership = _equipment_ownership(normalized)
    if ownership is not None:
        updated["equipment_ownership"] = ownership

    model = _equipment_model(raw, normalized)
    if model is not None:
        updated["equipment_model"] = model
        updated["equipment_model_known"] = True

    quantity = _equipment_quantity(normalized)
    if quantity is not None:
        updated["equipment_quantity"] = quantity
        if "quantity" not in updated:
            updated["quantity"] = quantity

    profile_changed = False
    area = _room_area(normalized)
    if area is not None:
        profile_changed = updated.get("room_area_m2") != area or profile_changed
        updated["room_area_m2"] = area

    people = _people_count(normalized)
    if people is not None:
        profile_changed = updated.get("room_people_max") != people or profile_changed
        updated["room_people_max"] = people

    preference = _preference(normalized)
    if preference is not None:
        profile_changed = updated.get("equipment_preference") != preference or profile_changed
        updated["equipment_preference"] = preference

    cycle = _equipment_cycle(normalized)
    if cycle is not None:
        profile_changed = updated.get("equipment_cycle") != cycle or profile_changed
        updated["equipment_cycle"] = cycle

    space_facts = _space_constraints(raw, normalized)
    if space_facts:
        updated.update(space_facts)

    corrected_address = _corrected_service_address(updated, raw, normalized)
    if corrected_address is not None:
        updated["service_address"] = corrected_address
        # A new locality invalidates any slot selected using the old route.
        updated.pop("selected_date", None)
        updated.pop("selected_time", None)
        updated.pop("candidate_booking", None)

    height = _height_over_three_meters(normalized)
    if height is not None:
        updated["installation_height_over_3m"] = height
        updated["work_at_height"] = height

    property_type = _property_type(normalized)
    if property_type is not None:
        updated["property_type"] = property_type

    hours = _hours_window(raw)
    if hours is not None:
        updated["building_hours_start"], updated["building_hours_end"] = hours

    if any(phrase in normalized for phrase in _SELF_CONTACT_PHRASES):
        updated["onsite_contact_mode"] = "customer"
    elif any(
        normalize_portuguese(phrase) in normalized
        for phrase in _OTHER_CONTACT_PHRASES
    ):
        updated["onsite_contact_mode"] = "other"
        name = _onsite_contact_name(raw)
        if name is not None:
            updated["onsite_contact_name"] = name

    if (
        updated.get("property_type") in {"building", "condominium"}
        and any(
            token in normalized
            for token in ("portaria", "bloco", "apto", "apartamento", "torre")
        )
    ):
        if len(raw) <= 300:
            updated["gate_instructions"] = raw

    phone = _phone(raw)
    if phone is not None and any(
        token in normalized
        for token in ("telefone", "contato", "whatsapp", "numero")
    ):
        updated["contact_phone"] = phone

    if whatsapp_id and "contact_phone" not in updated:
        normalized_phone = _normalize_whatsapp_phone(whatsapp_id)
        if normalized_phone is not None:
            updated["whatsapp_contact_phone"] = normalized_phone

    profile_keys = {
        "room_area_m2",
        "room_people_max",
        "equipment_preference",
        "equipment_cycle",
    }
    if profile_keys & updated.keys() and "equipment_profile_started_at" not in updated:
        updated["equipment_profile_started_at"] = datetime.now(UTC).isoformat()
    if profile_changed:
        updated["equipment_profile_last_answer_at"] = datetime.now(UTC).isoformat()

    return updated


def missing_equipment_profile_fields(context: dict[str, Any]) -> tuple[str, ...]:
    fields: list[str] = []
    if not isinstance(context.get("room_people_max"), int):
        fields.append("people")
    if not isinstance(context.get("room_area_m2"), (int, float)):
        fields.append("area")
    if context.get("equipment_cycle") not in {"cooling_only", "heat_cool"}:
        fields.append("cycle")
    if context.get("indoor_space_status") not in {
        "ample",
        "limited",
        "measured",
    }:
        fields.append("indoor_space")
    if context.get("outdoor_space_status") not in {
        "ample",
        "limited",
        "measured",
        "technical_balcony",
    }:
        fields.append("outdoor_space")
    if context.get("equipment_preference") not in {
        "modern",
        "cost_benefit",
        "economy",
    }:
        fields.append("preference")
    return tuple(fields)


def _equipment_cycle(normalized: str) -> str | None:
    if any(
        phrase in normalized
        for phrase in (
            "quente e frio",
            "quente/frio",
            "quente frio",
            "ciclo reverso",
            "aquecer e resfriar",
        )
    ):
        return "heat_cool"
    if any(
        phrase in normalized
        for phrase in (
            "somente frio",
            "so frio",
            "apenas frio",
            "ciclo frio",
        )
    ):
        return "cooling_only"
    return None


def _space_constraints(raw: str, normalized: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    indoor_tokens = (
        "evaporadora",
        "unidade interna",
        "espaco interno",
        "parede interna",
        "nicho interno",
    )
    outdoor_tokens = (
        "condensadora",
        "unidade externa",
        "espaco externo",
        "area externa",
        "varanda tecnica",
        "varanda",
    )
    clauses = tuple(
        normalize_portuguese(clause)
        for clause in re.split(
            r"[,;.]|\b(?:mas|enquanto)\b",
            raw,
            flags=re.IGNORECASE,
        )
        if normalize_portuguese(clause)
    )
    indoor_clause = _constraint_clause(clauses, indoor_tokens)
    outdoor_clause = _constraint_clause(clauses, outdoor_tokens)
    indoor_status = _constraint_status(indoor_clause)
    outdoor_status = _constraint_status(outdoor_clause, outdoor=True)

    if indoor_status is not None:
        facts["indoor_space_status"] = indoor_status
        facts["indoor_space_confirmed"] = True
        facts["indoor_space_details"] = raw[:300]
    if outdoor_status is not None:
        facts["outdoor_space_status"] = outdoor_status
        facts["outdoor_space_confirmed"] = True
        facts["outdoor_space_details"] = raw[:300]

    condenser_preferences = {
        "cilindrica": "cylindrical",
        "redonda": "cylindrical",
        "quadrada": "rectangular",
        "retangular": "rectangular",
        "compacta": "compact",
        "mais estreita": "narrow",
    }
    for phrase, value in condenser_preferences.items():
        if phrase in normalized and outdoor_clause is not None:
            facts["condenser_type_preference"] = value
            facts.setdefault("outdoor_space_status", "limited")
            facts["outdoor_space_confirmed"] = True
            facts["outdoor_space_details"] = raw[:300]
            break
    return facts


def _constraint_clause(
    clauses: tuple[str, ...],
    target_tokens: tuple[str, ...],
) -> str | None:
    matches = [
        clause
        for clause in clauses
        if any(token in clause for token in target_tokens)
    ]
    return " ".join(matches) if matches else None


def _constraint_status(clause: str | None, *, outdoor: bool = False) -> str | None:
    if clause is None:
        return None
    if outdoor and "varanda tecnica" in clause:
        return "technical_balcony"
    if any(
        phrase in clause
        for phrase in (
            "bem apertado",
            "espaco apertado",
            "pouco espaco",
            "espaco limitado",
            "nicho",
            "mais estreita",
            "pequeno",
            "pequena",
        )
    ):
        return "limited"
    if any(
        phrase in clause
        for phrase in (
            "bastante espaco",
            "espaco livre",
            "espaco amplo",
            "cabe tranquilo",
            "sem limitacao",
        )
    ):
        return "ample"
    if re.search(
        r"\b\d{1,4}(?:[.,]\d+)?\s*(?:x|por)\s*"
        r"\d{1,4}(?:[.,]\d+)?(?:\s*(?:x|por)\s*"
        r"\d{1,4}(?:[.,]\d+)?)?\s*(?:mm|cm|m)\b",
        clause,
    ):
        return "measured"
    return None


def _corrected_service_address(
    context: dict[str, Any],
    raw: str,
    normalized: str,
) -> dict[str, Any] | None:
    current = context.get("service_address")
    if not isinstance(current, dict) or not isinstance(current.get("city"), str):
        return None
    if not any(
        marker in normalized
        for marker in ("desculpa", "corrigindo", "correcao", "na verdade")
    ):
        return None
    match = re.search(
        r"(?:desculpa|corrigindo|correção|correcao|na verdade)"
        r"[^.!?]{0,50}?\b(?:é|e|fica em|cidade é|cidade e)\s+"
        r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{1,60})",
        raw,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    city = " ".join(match.group(1).strip(" .,;:!?").split())
    if not 1 <= len(city.split()) <= 6 or any(character.isdigit() for character in city):
        return None
    return {**current, "city": city}


def _equipment_ownership(normalized: str) -> str | None:
    has_brand = any(
        re.search(rf"\b{re.escape(brand)}\b", normalized)
        for brand in _BRANDS
    )
    has_equipment = (
        "ja tenho o ar" in normalized
        or "ja tenho um ar" in normalized
        or "ja tenho aparelho" in normalized
        or "tenho o aparelho" in normalized
        or ("ja tenho" in normalized and has_brand)
        or "so instalar" in normalized
        or "somente instalar" in normalized
        or "apenas instalar" in normalized
    )
    needs_equipment = (
        "nao tenho aparelho" in normalized
        or "nao tenho o ar" in normalized
        or "preciso comprar" in normalized
        or "quero comprar" in normalized
        or "comprar um ar" in normalized
        or "comprar o ar" in normalized
        or "cotacao do aparelho" in normalized
        or "orcamento do aparelho" in normalized
    )
    if needs_equipment:
        return "needs_equipment"
    if has_equipment:
        return "has_equipment"
    return None


def _equipment_model(raw: str, normalized: str) -> str | None:
    has_brand = any(
        re.search(rf"\b{re.escape(brand)}\b", normalized)
        for brand in _BRANDS
    )
    has_capacity = bool(
        re.search(r"\b(?:7|9|10|12|18|22|24|30|32|36|48|60)\s*(?:mil\s*)?btu", normalized)
        or re.search(r"\b(?:7000|9000|10000|12000|18000|22000|24000|30000|32000|36000|48000|60000)\s*btu", normalized)
    )
    explicit_model = bool(
        re.search(r"\bmodelo\b", normalized)
        and len(normalized.split()) <= 18
        and not any(
            phrase in normalized
            for phrase in (
                "nao tenho modelo",
                "sem modelo",
                "nenhum modelo",
                "nao sei o modelo",
            )
        )
    )
    if not (has_brand or has_capacity or explicit_model):
        return None
    return raw[:180]


def _equipment_quantity(normalized: str) -> int | None:
    match = re.search(
        r"\b([1-9]\d?)\s+(?:ares|aparelhos|equipamentos|splits|unidades)\b",
        normalized,
    )
    if match:
        return int(match.group(1))
    return None


def _room_area(normalized: str) -> float | None:
    match = re.search(
        r"\b(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:m2|m²|metros? quadrados?)\b",
        normalized,
    )
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value if 1 <= value <= 500 else None


def _people_count(normalized: str) -> int | None:
    match = re.search(
        r"\b([1-9]\d?)\s+(?:pessoa|pessoas|ocupantes)\b",
        normalized,
    )
    if match:
        value = int(match.group(1))
        return value if value <= 100 else None
    return None


def _preference(normalized: str) -> str | None:
    if any(phrase in normalized for phrase in _MODERN_PHRASES):
        return "modern"
    if any(phrase in normalized for phrase in _COST_BENEFIT_PHRASES):
        return "cost_benefit"
    if any(phrase in normalized for phrase in _ECONOMY_PHRASES):
        return "economy"
    return None


def _height_over_three_meters(normalized: str) -> bool | None:
    relevant = any(
        word in normalized
        for word in (
            "altura",
            "alto",
            "parede",
            "evaporadora",
            "condensadora",
            "unidade interna",
            "unidade externa",
        )
    )
    if not relevant:
        return None
    if any(
        phrase in normalized
        for phrase in (
            "mais de 3 metros",
            "acima de 3 metros",
            "passa de 3 metros",
            "superior a 3 metros",
        )
    ):
        return True
    if any(
        phrase in normalized
        for phrase in (
            "ate 3 metros",
            "menos de 3 metros",
            "abaixo de 3 metros",
            "nao passa de 3 metros",
        )
    ):
        return False
    match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:m|metro|metros)\b", normalized)
    if match:
        return float(match.group(1).replace(",", ".")) > 3
    return None


def _property_type(normalized: str) -> str | None:
    if re.search(r"\bcondominio\b", normalized):
        return "condominium"
    if re.search(r"\b(?:predio|edificio|apartamento|apto)\b", normalized):
        return "building"
    if re.search(r"\b(?:casa|residencia|residencial)\b", normalized):
        return "house"
    return None


def _hours_window(value: str) -> tuple[str, str] | None:
    match = re.search(
        r"\b(?:das?\s*)?(\d{1,2})(?::(\d{2}))?\s*(?:h|horas?)?"
        r"\s*(?:às|as|até|ate|a)\s*(\d{1,2})(?::(\d{2}))?"
        r"\s*(?:h|horas?)?\b",
        value.casefold(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = match.groups()
    start_h = int(start_hour)
    end_h = int(end_hour)
    start_m = int(start_minute or 0)
    end_m = int(end_minute or 0)
    if not (
        0 <= start_h <= 23
        and 0 <= end_h <= 23
        and 0 <= start_m <= 59
        and 0 <= end_m <= 59
    ):
        return None
    start_value = f"{start_h:02d}:{start_m:02d}"
    end_value = f"{end_h:02d}:{end_m:02d}"
    return (start_value, end_value) if start_value < end_value else None

def _onsite_contact_name(raw: str) -> str | None:
    patterns = (
        r"(?:minha esposa|meu marido|minha mãe|minha mae|meu pai|minha filha|meu filho)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\-]{1,40})",
        r"(?:quem vai estar|quem estará|vai estar|estara)\s+(?:e|é)?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{1,60})",
        r"(?:outra pessoa[,;:]?\s*)([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{1,60})",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            value = " ".join(match.group(1).strip(" .,;:!?").split())
            if 1 <= len(value.split()) <= 4:
                return value
    return None


def _phone(raw: str) -> str | None:
    match = _PHONE_PATTERN.search(raw)
    if not match:
        return None
    ddd, prefix, suffix = match.groups()
    return f"+55{ddd}{prefix}{suffix}"


def _normalize_whatsapp_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("55") and 12 <= len(digits) <= 13:
        return f"+{digits}"
    if 10 <= len(digits) <= 11:
        return f"+55{digits}"
    return None

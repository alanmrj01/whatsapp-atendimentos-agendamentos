from __future__ import annotations

import re
from typing import Any

from app.conversations.context_policy import invalidate_changed_facts
from app.conversations.interpreter import correction_focus, normalize_portuguese

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

_SIMPLE_NUMBERS = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "cem": 100,
}
_TENS = {
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50,
    "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
}


def parse_number_answer(value: str | None) -> float | None:
    """Parse a compact numeric answer in digits or common Portuguese words."""

    normalized = normalize_portuguese(value or "")
    if not normalized:
        return None
    digit = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,2})?)(?!\d)", normalized)
    if digit:
        return float(digit.group(1).replace(",", "."))

    tokens = normalized.split()
    for index, token in enumerate(tokens):
        if token in _SIMPLE_NUMBERS:
            return float(_SIMPLE_NUMBERS[token])
        if token in _TENS:
            number = _TENS[token]
            if (
                index + 2 < len(tokens)
                and tokens[index + 1] == "e"
                and tokens[index + 2] in _SIMPLE_NUMBERS
                and _SIMPLE_NUMBERS[tokens[index + 2]] < 10
            ):
                number += _SIMPLE_NUMBERS[tokens[index + 2]]
            return float(number)
    return None


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

    updated = dict(context)
    if whatsapp_id and "contact_phone" not in updated:
        normalized_phone = _normalize_whatsapp_phone(whatsapp_id)
        if normalized_phone is not None:
            updated["whatsapp_contact_phone"] = normalized_phone

    raw = " ".join((body or "").strip().split())
    if not raw:
        return updated

    normalized = normalize_portuguese(raw)
    assertion = correction_focus(raw)
    is_correction = assertion != normalized or any(
        marker in normalized
        for marker in ("na verdade", "quis dizer", "corrigindo", "melhor dizendo")
    )
    if any(phrase in normalized for phrase in _QUOTE_PHRASES):
        updated["request_mode"] = "quote"

    ownership = _equipment_ownership(assertion)
    if ownership is not None:
        _assign_fact(updated, "equipment_ownership", ownership, is_correction)

    model = _equipment_model(raw, assertion)
    if model is not None:
        _assign_fact(updated, "equipment_model", model, is_correction)
        updated["equipment_model_known"] = True

    quantity = _equipment_quantity(assertion)
    if quantity is not None:
        _assign_fact(updated, "equipment_quantity", quantity, is_correction)
        if "quantity" not in updated:
            updated["quantity"] = quantity

    area = _room_area(assertion)
    if (
        area is None
        and is_correction
        and any(
            token in normalized
            for token in ("m2", "metros quadrados", "area", "tamanho", "ambiente")
        )
    ):
        corrected_area = parse_number_answer(assertion)
        if corrected_area is not None and 1 <= corrected_area <= 1000:
            area = corrected_area
    if area is not None:
        _assign_fact(updated, "room_area_m2", area, is_correction)

    people = _people_count(assertion)
    if (
        people is None
        and is_correction
        and any(token in normalized for token in ("pessoa", "pessoas", "ocupantes"))
    ):
        corrected_number = parse_number_answer(assertion)
        if (
            corrected_number is not None
            and corrected_number.is_integer()
            and 1 <= corrected_number <= 100
        ):
            people = int(corrected_number)
    if people is not None:
        _assign_fact(updated, "room_people_max", people, is_correction)

    preference = _preference(assertion)
    if preference is not None:
        _assign_fact(updated, "equipment_preference", preference, is_correction)

    cycle = _climate_mode(assertion)
    if cycle is not None:
        _assign_fact(updated, "equipment_cycle", cycle, is_correction)

    space = _installation_space(raw, assertion)
    if space is not None:
        target, values = space
        if values == "unrestricted":
            updated[f"{target}_space_unrestricted"] = True
        else:
            width, height_cm, depth = values
            updated[f"{target}_space_width_cm"] = width
            updated[f"{target}_space_height_cm"] = height_cm
            if depth is not None:
                updated[f"{target}_space_depth_cm"] = depth
            updated[f"{target}_space_unrestricted"] = False

    height = _height_over_three_meters(assertion)
    if height is not None:
        updated["installation_height_over_3m"] = height
        updated["work_at_height"] = height

    property_type = _property_type(assertion)
    if property_type is not None:
        _assign_fact(updated, "property_type", property_type, is_correction)

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

    return invalidate_changed_facts(context, updated)


def _assign_fact(
    context: dict[str, Any],
    key: str,
    value: Any,
    is_correction: bool,
) -> None:
    existing = context.get(key)
    if existing is None or existing == value or is_correction:
        context[key] = value


def missing_equipment_profile_fields(context: dict[str, Any]) -> tuple[str, ...]:
    fields: list[str] = []
    if not isinstance(context.get("room_people_max"), int):
        fields.append("people")
    if not isinstance(context.get("room_area_m2"), (int, float)):
        fields.append("area")
    if context.get("equipment_preference") not in {
        "modern",
        "cost_benefit",
        "economy",
    }:
        fields.append("preference")
    if context.get("equipment_cycle") not in {"cold", "heat_cool"}:
        fields.append("cycle")
    if not _space_known(context, "indoor"):
        fields.append("indoor_space")
    if not _space_known(context, "outdoor"):
        fields.append("outdoor_space")
    return tuple(fields)


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
    if match:
        value = float(match.group(1).replace(",", "."))
        return value if 1 <= value <= 500 else None
    if re.search(r"\b(?:m2|m²|metros? quadrados?|metros? de area)\b", normalized):
        value = parse_number_answer(normalized)
        if value is not None and 1 <= value <= 500:
            return value
    return None


def _people_count(normalized: str) -> int | None:
    match = re.search(
        r"\b([1-9]\d?)\s+(?:pessoa|pessoas|ocupantes)\b",
        normalized,
    )
    if match:
        value = int(match.group(1))
        return value if value <= 100 else None
    if re.search(r"\b(?:pessoa|pessoas|ocupantes)\b", normalized):
        value = parse_number_answer(normalized)
        if value is not None and value.is_integer() and 1 <= value <= 100:
            return int(value)
    return None


def _preference(normalized: str) -> str | None:
    if any(phrase in normalized for phrase in _MODERN_PHRASES):
        return "modern"
    if any(phrase in normalized for phrase in _COST_BENEFIT_PHRASES):
        return "cost_benefit"
    if any(phrase in normalized for phrase in _ECONOMY_PHRASES):
        return "economy"
    return None


def _climate_mode(normalized: str) -> str | None:
    if any(
        phrase in normalized
        for phrase in (
            "quente frio",
            "quente e frio",
            "aquecer e gelar",
            "aquecer tambem",
            "tambem aqueca",
            "tambem aquece",
            "ciclo reverso",
        )
    ):
        return "heat_cool"
    if any(
        phrase in normalized
        for phrase in (
            "so frio",
            "somente frio",
            "apenas frio",
            "so gelar",
            "apenas gelar",
            "somente gelar",
            "nao precisa aquecer",
        )
    ):
        return "cold"
    return None


def _installation_space(
    raw: str,
    normalized: str,
) -> tuple[str, tuple[float, float, float | None] | str] | None:
    target: str | None = None
    if any(
        token in normalized
        for token in ("unidade interna", "evaporadora", "espaco interno", "parede interna")
    ):
        target = "indoor"
    elif any(
        token in normalized
        for token in ("unidade externa", "condensadora", "espaco externo", "area externa")
    ):
        target = "outdoor"
    if target is None:
        return None

    if any(
        phrase in normalized
        for phrase in (
            "sem limitacao",
            "sem restricao",
            "tem bastante espaco",
            "espaco livre",
            "nao tem problema de espaco",
        )
    ):
        return target, "unrestricted"

    values = [
        float(value.replace(",", "."))
        for value in re.findall(
            r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:cm|centimetros?)?",
            raw.casefold(),
        )
    ]
    if len(values) >= 2:
        return target, (
            values[0],
            values[1],
            values[2] if len(values) >= 3 else None,
        )
    return None


def _space_known(context: dict[str, Any], target: str) -> bool:
    if context.get(f"{target}_space_unrestricted") is True:
        return True
    return (
        isinstance(context.get(f"{target}_space_width_cm"), (int, float))
        and isinstance(context.get(f"{target}_space_height_cm"), (int, float))
    )


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
        r"\b(?:das?\s*)?(\d{1,2})(?::(\d{2}))?\s*(?:h|hs|hr|hrs|hora|horas)?"
        r"\s*(?:às|as|até|ate|a|-|–|—)\s*(\d{1,2})(?::(\d{2}))?"
        r"\s*(?:h|hs|hr|hrs|hora|horas)?\b",
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

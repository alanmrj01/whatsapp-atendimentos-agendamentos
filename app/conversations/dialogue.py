from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.conversations.ports import BookingOption
from app.conversations.interpreter import normalize_portuguese

PORTUGUESE_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)

PORTUGUESE_WEEKDAY_PLURALS = (
    "segundas-feiras",
    "terças-feiras",
    "quartas-feiras",
    "quintas-feiras",
    "sextas-feiras",
    "sábados",
    "domingos",
)

_WEEKDAY_ALIASES: dict[int, tuple[str, ...]] = {
    0: ("segunda", "segunda feira", "seg"),
    1: ("terca", "terca feira", "ter"),
    2: ("quarta", "quarta feira", "qua"),
    3: ("quinta", "quinta feira", "qui"),
    4: ("sexta", "sexta feira", "sex"),
    5: ("sabado", "sab"),
    6: ("domingo", "dom"),
}


def daypart_greeting(
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("America/Sao_Paulo")
    local = current.astimezone(zone)
    if 5 <= local.hour < 12:
        return "Bom dia"
    if 12 <= local.hour < 18:
        return "Boa tarde"
    return "Boa noite"


def weekday_from_text(value: str | None) -> int | None:
    normalized = normalize_portuguese(value or "")
    if not normalized:
        return None
    padded = f" {normalized} "
    matches = [
        index
        for index, aliases in _WEEKDAY_ALIASES.items()
        if any(f" {alias} " in padded for alias in aliases)
    ]
    return matches[0] if len(matches) == 1 else None


def weekday_options(dates: Sequence[BookingOption]) -> tuple[BookingOption, ...]:
    seen: set[int] = set()
    options: list[BookingOption] = []
    for option in dates:
        weekday = date.fromisoformat(option.id).weekday()
        if weekday in seen:
            continue
        seen.add(weekday)
        options.append(
            BookingOption(
                id=str(weekday),
                label=PORTUGUESE_WEEKDAYS[weekday].capitalize(),
            )
        )
    return tuple(options)


def dates_for_weekday(
    dates: Sequence[BookingOption],
    weekday: int,
) -> tuple[BookingOption, ...]:
    return tuple(
        option
        for option in dates
        if date.fromisoformat(option.id).weekday() == weekday
    )


def date_short_label(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{PORTUGUESE_WEEKDAYS[parsed.weekday()]}, {parsed.strftime('%d/%m')}"


def customer_lead(name: str | None) -> str:
    return f"{name}, " if name else ""


def weekday_plural(weekday: int) -> str:
    return PORTUGUESE_WEEKDAY_PLURALS[weekday]

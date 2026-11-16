"""Round 2D.2: natural schedule language constrained to offered options."""

from unittest.mock import AsyncMock

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
)
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


@pytest.mark.parametrize(
    "body",
    [
        "tem vaga pra essa semana?",
        "qnd vcs conseguem vir?",
        "qual dia dá pra atender?",
        "tem algum horário livre?",
        "e agenda, como tá?",
        "TEM HORARIO DISPONIVEL?",
        "tem hr livre hj?",
        "oxe, quando cês podem vir?",
        "uai, tem vaga na sexta?",
        "blz, qual o próximo horário?",
        "📅 tem disponibilidade?",
    ],
)
def test_real_whatsapp_availability_language_is_understood(body: str) -> None:
    interpretation = DeterministicConversationInterpreter().interpret(body)

    assert interpretation.has(ConversationIntent.AVAILABILITY)


@pytest.mark.asyncio
async def test_initial_schedule_prompt_uses_all_configured_company_weekdays() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SERVICE,
        customer_name="Cliente Teste",
    )
    port = FakeBookingPort()
    port.operating_weekdays = [0, 1, 2, 3, 4, 5, 6]
    port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
    ]

    await ConversationEngine(repository, port).process(
        inbound(2298, action=f"service:{SERVICE_ID}")
    )

    assert repository.state == ConversationState.BOOKING_WEEKDAY
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == [
        "weekday:0",
        "weekday:1",
        "weekday:2",
        "weekday:3",
        "weekday:4",
        "weekday:5",
        "weekday:6",
    ]


@pytest.mark.asyncio
async def test_selected_weekday_can_offer_three_dates_across_months() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Cliente Teste",
    )
    port = FakeBookingPort()
    port.operating_weekdays = [0, 1, 2, 3, 4, 5, 6]
    port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
        BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        BookingOption("2026-11-04", "quarta-feira, 4 de novembro"),
        BookingOption("2026-11-06", "sexta-feira, 6 de novembro"),
    ]
    port.list_times = AsyncMock(
        return_value=(
            BookingOption("09:00", "09:00"),
            BookingOption("14:30", "14:30"),
            BookingOption("18:00", "18:00"),
        )
    )

    await ConversationEngine(repository, port).process(
        inbound(2299, body="quarta-feira")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == [
        "slot:2026-10-14|09:00",
        "slot:2026-10-21|14:30",
        "slot:2026-11-04|18:00",
    ]


def _date_repository() -> FakeConversationRepository:
    return FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Cliente Teste",
    )


def _date_port(dates: list[BookingOption]) -> FakeBookingPort:
    port = FakeBookingPort()
    port.dates = dates
    port.list_times = AsyncMock(
        return_value=(
            BookingOption("09:00", "09:00"),
            BookingOption("14:30", "14:30"),
            BookingOption("18:00", "18:00"),
        )
    )
    return port


@pytest.mark.parametrize(
    "body",
    [
        "14/10",
        "14-10",
        "dia 14/10",
        "quarta, 14 de outubro",
        "14 de outubro",
    ],
)
@pytest.mark.asyncio
async def test_explicit_date_variants_select_only_matching_offered_month(
    body: str,
) -> None:
    repository = _date_repository()
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
            BookingOption("2026-11-14", "sábado, 14 de novembro"),
        ]
    )

    await ConversationEngine(repository, port).process(inbound(2300, body=body))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert "selected_time" not in repository.context


@pytest.mark.asyncio
async def test_day_number_selects_date_when_only_one_offered_candidate_exists() -> None:
    repository = _date_repository()
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(
        inbound(2301, body="pode ser dia 14")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"


@pytest.mark.asyncio
async def test_unoffered_date_never_advances_or_creates_selection() -> None:
    repository = _date_repository()
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(inbound(2302, body="dia 20"))

    assert repository.state == ConversationState.BOOKING_DATE
    assert "selected_date" not in repository.context


@pytest.mark.asyncio
async def test_ambiguous_day_number_never_selects_first_offered_date() -> None:
    repository = _date_repository()
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-11-14", "sábado, 14 de novembro"),
        ]
    )

    await ConversationEngine(repository, port).process(inbound(2303, body="dia 14"))

    assert repository.state == ConversationState.BOOKING_DATE
    assert "selected_date" not in repository.context


@pytest.mark.parametrize(
    "body",
    ["quarta", "quarta-feira", "pode ser na quarta", "na quarta eu consigo"],
)
@pytest.mark.asyncio
async def test_weekday_with_two_offered_dates_only_filters_real_options(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Cliente Teste",
    )
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
            BookingOption("2026-10-16", "sexta-feira, 16 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(inbound(2310, body=body))

    assert repository.state == ConversationState.BOOKING_DATE
    assert "selected_date" not in repository.context
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == [
        "slot:2026-10-14|09:00",
        "slot:2026-10-21|14:30",
    ]


@pytest.mark.asyncio
async def test_weekday_with_one_offered_date_still_offers_real_combined_slot() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
    )
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-16", "sexta-feira, 16 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(inbound(2311, body="quarta"))

    assert repository.state == ConversationState.BOOKING_DATE
    assert "selected_date" not in repository.context
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == ["slot:2026-10-14|09:00"]


@pytest.mark.asyncio
async def test_any_day_offers_three_nearest_real_slots_with_varied_times() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Cliente Teste",
    )
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-16", "sexta-feira, 16 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
            BookingOption("2026-10-23", "sexta-feira, 23 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(
        inbound(2312, body="qualquer dia serve")
    )

    assert repository.state == ConversationState.BOOKING_DATE
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == [
        "slot:2026-10-14|09:00",
        "slot:2026-10-16|14:30",
        "slot:2026-10-21|18:00",
    ]


@pytest.mark.asyncio
async def test_combined_slot_is_revalidated_before_advancing() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
        customer_name="Cliente Teste",
    )
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(
        inbound(2313, action="slot:2026-10-14|14:30", body="quarta, 14/10 às 14:30")
    )

    assert repository.context["selected_date"] == "2026-10-14"
    assert repository.context["selected_time"] == "14:30"
    assert repository.state not in {
        ConversationState.BOOKING_DATE,
        ConversationState.BOOKING_TIME,
    }


def _time_scenario(
    times: list[BookingOption],
) -> tuple[FakeConversationRepository, FakeBookingPort, ConversationEngine]:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-10-14",
        },
        customer_name="Cliente Teste",
    )
    port = FakeBookingPort()
    port.times = times
    port.list_times = AsyncMock(return_value=tuple(times))
    return repository, port, ConversationEngine(repository, port)


@pytest.mark.parametrize(
    ("body", "expected", "slots"),
    [
        ("9h", "09:00", ["09:00", "14:30"]),
        ("09:00", "09:00", ["09:00", "14:30"]),
        ("às 9", "09:00", ["09:00", "14:30"]),
        ("pode ser nove horas", "09:00", ["09:00", "14:30"]),
        ("14h30", "14:30", ["09:00", "14:30"]),
        ("14:30", "14:30", ["09:00", "14:30"]),
        ("duas e meia da tarde", "14:30", ["09:00", "14:30"]),
        ("duas da tarde", "14:00", ["02:00", "14:00"]),
        ("oito da noite", "20:00", ["08:00", "20:00"]),
        ("meio-dia", "12:00", ["09:00", "12:00"]),
        ("meia-noite", "00:00", ["00:00", "09:00"]),
        ("nove e meia", "09:30", ["09:30", "14:30"]),
        ("duas e meia", "14:30", ["14:30", "09:00"]),
    ],
)
@pytest.mark.asyncio
async def test_natural_time_selects_only_a_matching_offered_slot(
    body: str,
    expected: str,
    slots: list[str],
) -> None:
    options = [BookingOption(value, value) for value in slots]
    repository, port, engine = _time_scenario(options)

    await engine.process(inbound(2320, body=body))

    assert repository.state != ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert repository.context["selected_time"] == expected
    assert port.confirmations == []


@pytest.mark.asyncio
async def test_ambiguous_natural_time_never_selects_first_offered_slot() -> None:
    repository, port, engine = _time_scenario(
        [
            BookingOption("02:30", "02:30"),
            BookingOption("14:30", "14:30"),
        ]
    )

    await engine.process(inbound(2321, body="duas e meia"))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert "selected_time" not in repository.context
    assert port.confirmations == []


@pytest.mark.asyncio
async def test_unoffered_natural_time_keeps_selected_date_and_real_options() -> None:
    repository, port, engine = _time_scenario(
        [BookingOption("09:00", "09:00"), BookingOption("14:30", "14:30")]
    )

    await engine.process(inbound(2322, body="seis da tarde"))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert "selected_time" not in repository.context
    assert port.confirmations == []
    response = repository.outbounds[-1].transition.outbound.body or ""
    assert "09:00" in response and "14:30" in response


@pytest.mark.parametrize(
    "body",
    ["qualquer horário serve", "pode escolher por mim", "o primeiro que tiver"],
)
@pytest.mark.asyncio
async def test_vague_time_never_selects_or_confirms_a_slot(body: str) -> None:
    repository, port, engine = _time_scenario(
        [BookingOption("09:00", "09:00"), BookingOption("14:30", "14:30")]
    )

    await engine.process(inbound(2330, body=body))

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert "selected_time" not in repository.context
    assert port.confirmations == []


@pytest.mark.parametrize(
    ("state", "context", "body", "resume_fragment"),
    [
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "quanto custa antes de eu escolher o dia?",
            "r$ 100,00",
        ),
        (
            ConversationState.BOOKING_TIME,
            {"service_id": str(SERVICE_ID), "selected_date": "2026-10-14"},
            "quanto tempo demora o serviço?",
            "30 minutos",
        ),
    ],
)
@pytest.mark.asyncio
async def test_side_question_preserves_schedule_checkpoint(
    state: ConversationState,
    context: dict[str, str],
    body: str,
    resume_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        ]
    )
    port.times = [BookingOption("09:00", "09:00")]

    await ConversationEngine(repository, port).process(inbound(2340, body=body))

    assert repository.state == state
    assert repository.context.get("selected_date") == context.get("selected_date")
    assert "selected_time" not in repository.context
    assert "repair_attempts" not in repository.context
    assert resume_fragment in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@pytest.mark.asyncio
async def test_late_date_correction_from_round2b_remains_valid() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-10-14",
            "selected_time": "09:00",
            "service_address": "Rua Sintética, 10, Centro",
        },
    )
    port = _date_port(
        [
            BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
            BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        ]
    )

    await ConversationEngine(repository, port).process(
        inbound(2350, body="não é dia 14, é dia 21/10")
    )

    assert repository.context["selected_date"] == "2026-10-21"
    assert "selected_time" not in repository.context
    assert repository.context["service_address"] == "Rua Sintética, 10, Centro"
    assert repository.state == ConversationState.BOOKING_TIME


@pytest.mark.asyncio
async def test_availability_during_site_limit_never_becomes_restriction() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_SITE_LIMIT,
        context={"service_id": str(SERVICE_ID), "quantity": 2},
    )
    port = FakeBookingPort()

    await ConversationEngine(repository, port).process(
        inbound(2360, body="tem horário até 16h?")
    )

    assert repository.state == ConversationState.BOOKING_SITE_LIMIT
    assert "site_limit_answered" not in repository.context
    assert "site_allowed_end" not in repository.context

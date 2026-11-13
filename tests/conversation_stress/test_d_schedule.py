from __future__ import annotations

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


def test_explicit_request_to_book_tomorrow_is_a_booking_intent() -> None:
    interpretation = DeterministicConversationInterpreter().interpret(
        "consigo marcar pra amanhã?"
    )

    assert interpretation.has(ConversationIntent.BOOK)


@pytest.mark.parametrize(
    "body",
    [
        "quarta",
        "quarta-feira",
        "pode ser na quarta",
        "na quarta eu consigo",
    ],
)
@pytest.mark.asyncio
async def test_weekday_variants_narrow_only_the_offered_dates(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_WEEKDAY,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
        BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
        BookingOption("2026-10-16", "sexta-feira, 16 de outubro"),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(100, body=body)
    )

    assert repository.state == ConversationState.BOOKING_DATE
    assert "selected_date" not in repository.context
    rows = repository.outbounds[-1].transition.outbound.outbound_payload[
        "sections"
    ][0]["rows"]
    assert [row["id"] for row in rows] == [
        "date:2026-10-14",
        "date:2026-10-21",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "14/10",
        "14-10",
        "dia 14/10",
        "quarta, 14 de outubro",
        "14 de outubro",
        "pode ser dia 14",
    ],
)
@pytest.mark.asyncio
async def test_date_variants_consume_only_an_offered_date(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_DATE,
        context={"service_id": str(SERVICE_ID)},
    )
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
        BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
    ]
    booking_port.list_times = AsyncMock(
        return_value=(
            BookingOption("09:00", "09:00"),
            BookingOption("14:30", "14:30"),
        )
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(200, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-10-14"
    assert "selected_time" not in repository.context


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("9h", "09:00"),
        ("09:00", "09:00"),
        ("às 9", "09:00"),
        ("pode ser nove horas", "09:00"),
        ("14h30", "14:30"),
        ("14:30", "14:30"),
        ("duas e meia da tarde", "14:30"),
    ],
)
@pytest.mark.asyncio
async def test_time_variants_consume_only_an_offered_slot(
    body: str, expected: str
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.times = [
        BookingOption("09:00", "09:00"),
        BookingOption("14:30", "14:30"),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(300, body=body)
    )

    assert repository.state == ConversationState.BOOKING_ATTENDEE
    assert repository.context["selected_date"] == "2026-09-02"
    assert repository.context["selected_time"] == expected
    assert booking_port.confirmations == []


@pytest.mark.parametrize(
    ("state", "context", "body", "expected_fragment"),
    [
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "quanto custa antes de eu escolher o dia?",
            "r$ 100,00",
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "quanto tempo demora o serviço?",
            "30 minutos",
        ),
        (
            ConversationState.BOOKING_TIME,
            {"service_id": str(SERVICE_ID), "selected_date": "2026-09-02"},
            "o que está incluso nesse valor?",
            "09:00",
        ),
        (
            ConversationState.BOOKING_TIME,
            {"service_id": str(SERVICE_ID), "selected_date": "2026-09-02"},
            "essa visita tem garantia?",
            "09:00",
        ),
    ],
)
@pytest.mark.asyncio
async def test_side_question_preserves_and_resumes_schedule_checkpoint(
    state: ConversationState,
    context: dict[str, str],
    body: str,
    expected_fragment: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()

    await ConversationEngine(repository, booking_port).process(
        inbound(400, body=body)
    )

    assert repository.state == state
    assert repository.context.get("service_id") == str(SERVICE_ID)
    assert repository.context.get("selected_date") == context.get("selected_date")
    assert "selected_time" not in repository.context
    assert "repair_attempts" not in repository.context
    assert expected_fragment in (
        repository.outbounds[-1].transition.outbound.body or ""
    ).casefold()


@pytest.mark.parametrize(
    "body",
    [
        "qualquer horário serve",
        "pode escolher por mim",
        "o primeiro que tiver",
    ],
)
@pytest.mark.asyncio
async def test_vague_time_never_invents_or_confirms_a_slot(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.times = [
        BookingOption("09:00", "09:00"),
        BookingOption("14:30", "14:30"),
    ]

    await ConversationEngine(repository, booking_port).process(
        inbound(500, body=body)
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert "selected_time" not in repository.context
    assert booking_port.confirmations == []


@pytest.mark.asyncio
async def test_unavailable_time_does_not_replace_the_selected_date() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.times = [BookingOption("09:00", "09:00")]

    await ConversationEngine(repository, booking_port).process(
        inbound(501, body="quero às 18h")
    )

    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context["selected_date"] == "2026-09-02"
    assert "selected_time" not in repository.context
    assert booking_port.confirmations == []
    assert "09:00" in (repository.outbounds[-1].transition.outbound.body or "")


@pytest.mark.asyncio
async def test_date_correction_replaces_only_date_dependent_facts() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-10-14",
            "selected_time": "09:00",
            "service_address": "Rua Sintética, 10, Centro",
        },
    )
    booking_port = FakeBookingPort()
    booking_port.dates = [
        BookingOption("2026-10-14", "quarta-feira, 14 de outubro"),
        BookingOption("2026-10-21", "quarta-feira, 21 de outubro"),
    ]
    booking_port.list_times = AsyncMock(
        return_value=(BookingOption("14:30", "14:30"),)
    )

    await ConversationEngine(repository, booking_port).process(
        inbound(502, body="não é dia 14, é dia 21/10")
    )

    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["service_address"] == "Rua Sintética, 10, Centro"
    assert repository.context["selected_date"] == "2026-10-21"
    assert "selected_time" not in repository.context
    assert repository.state == ConversationState.BOOKING_TIME


@pytest.mark.asyncio
async def test_repeated_unknown_during_time_selection_is_bounded() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_TIME,
        context={
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
        },
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    await engine.process(inbound(600, body="talvez, sei lá"))
    await engine.process(inbound(601, body="não faço ideia"))
    await engine.process(inbound(602, body="depois eu vejo"))

    assert 1 <= len(repository.outbounds) <= 3
    assert repository.state in {
        ConversationState.BOOKING_TIME,
        ConversationState.HUMAN_HANDOFF,
    }
    assert not (
        repository.state == ConversationState.BOOKING_TIME
        and repository.context.get("repair_attempts", {}).get("time", 0) > 2
    )

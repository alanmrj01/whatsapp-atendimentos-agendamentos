"""Round 2D.1: locality and property-hour semantic guards.

The corpus captures product invariants before runtime changes.  It deliberately
keeps property restrictions separate from requested appointment times.
"""

from dataclasses import replace
import unicodedata

import pytest

from app.booking.domain import PricingType
from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


SERVICE_ADDRESS = {
    "address_line": "Rua das Flores, 10, Centro",
    "city": "São José dos Campos",
    "state": "SP",
}

OLD_ADDRESS = {
    "address_line": "Rua Antiga, 10, Centro",
    "city": "São José dos Campos",
    "state": "SP",
}


def _scenario(
    state: str,
    *,
    context: dict | None = None,
    site_limit: bool = False,
) -> tuple[FakeConversationRepository, ConversationEngine]:
    repository = FakeConversationRepository(
        state=state,
        context={"service_id": str(SERVICE_ID), **(context or {})},
        customer_name="Cliente Teste",
    )
    booking = FakeBookingPort()
    booking.intake = replace(
        booking.intake,
        requires_address=True,
        asks_site_time_limit=site_limit,
        pricing_type=PricingType.ESTIMATED,
    )
    booking.business_city = "São José dos Campos"
    booking.business_state = "SP"
    return repository, ConversationEngine(repository, booking)


def _last_body(repository: FakeConversationRepository) -> str:
    return (repository.outbounds[-1].transition.outbound.body or "").casefold()


def _normalized(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


@pytest.mark.parametrize(
    ("body", "expected_line", "city", "state"),
    [
        (
            "Rua das Flores, 123, Centro, São José dos Campos - SP",
            "Rua das Flores, 123, Centro",
            "São José dos Campos",
            "SP",
        ),
        (
            "Rua das Flores 123, Centro, São José dos Campos/SP",
            "Rua das Flores 123, Centro",
            "São José dos Campos",
            "SP",
        ),
        (
            "Rua das Flores, 123, Centro, SJC",
            "Rua das Flores, 123, Centro",
            "São José dos Campos",
            "SP",
        ),
        (
            "Rua das Flores 123, Jardim Aquarius, sjc",
            "Rua das Flores 123, Jardim Aquarius",
            "São José dos Campos",
            "SP",
        ),
        (
            "Rua das Flores 123, Jardim Aquarius, SJC/SP",
            "Rua das Flores 123, Jardim Aquarius",
            "São José dos Campos",
            "SP",
        ),
        (
            "Rua das Flores 123, Jardim Aquarius, sjc-sp",
            "Rua das Flores 123, Jardim Aquarius",
            "São José dos Campos",
            "SP",
        ),
        (
            "Alameda dos Ipês, 88, Urbanova, S. J. dos Campos",
            "Alameda dos Ipês, 88, Urbanova",
            "São José dos Campos",
            "SP",
        ),
        (
            "Av. Brasil, 500, Centro, Jacareí - SP",
            "Avenida Brasil, 500, Centro",
            "Jacareí",
            "SP",
        ),
        (
            "Rua X, 25, Centro, Taubaté/SP",
            "Rua X, 25, Centro",
            "Taubaté",
            "SP",
        ),
        (
            "Rua Projetada, 42, Centro, Caçapava SP",
            "Rua Projetada, 42, Centro",
            "Caçapava",
            "SP",
        ),
    ],
    ids=[
        "hyphen",
        "slash",
        "sjc",
        "lowercase-sjc",
        "sjc-slash-state",
        "sjc-hyphen-state",
        "dotted-sjc",
        "different-city",
        "different-city-slash",
        "city-space-state",
    ],
)
@pytest.mark.asyncio
async def test_complete_addresses_canonicalize_line_city_and_state_without_duplication(
    body: str,
    expected_line: str,
    city: str,
    state: str,
) -> None:
    repository, engine = _scenario(ConversationState.BOOKING_ADDRESS)

    await engine.process(inbound(2000, body=body))

    address = repository.context["service_address"]
    assert address == {
        "address_line": expected_line,
        "city": city,
        "state": state,
    }
    normalized_line = _normalized(address["address_line"])
    assert _normalized(city) not in normalized_line
    assert not normalized_line.endswith(f" {state.casefold()}")
    assert repository.state != ConversationState.BOOKING_ADDRESS


@pytest.mark.parametrize(
    "body",
    [
        "Rua X, 123, Centro, SP",
        "Rua Nova, 123",
        "Rua das Acácias, 80, Jardim Aquarius",
    ],
    ids=["isolated-state", "street-number-only", "neighborhood-is-not-city"],
)
@pytest.mark.asyncio
async def test_incomplete_locality_preserves_pending_address_and_requests_city(
    body: str,
) -> None:
    repository, engine = _scenario(ConversationState.BOOKING_ADDRESS)

    await engine.process(inbound(2010, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["awaiting_address_city"] is True
    assert repository.context["pending_service_address"]
    assert "service_address" not in repository.context
    assert "cidade" in _last_body(repository)


@pytest.mark.parametrize("body", ["uai", "oxe", "bah", "mano"])
@pytest.mark.asyncio
async def test_regionalism_never_defines_locality(body: str) -> None:
    repository, engine = _scenario(ConversationState.BOOKING_ADDRESS)

    await engine.process(inbound(2020, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert "service_address" not in repository.context
    assert "pending_service_address" not in repository.context


@pytest.mark.asyncio
async def test_incomplete_late_address_correction_discards_old_route_and_city() -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_TIME,
        context={
            "quantity": 2,
            "property_type": "house",
            "service_address": OLD_ADDRESS,
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
            "candidate_booking": {"service_id": str(SERVICE_ID)},
            "quote_presented": True,
        },
    )

    await engine.process(
        inbound(2030, body="corrigindo, o endereço é Rua Nova, 123")
    )

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["pending_service_address"] == "Rua Nova, 123"
    assert "service_address" not in repository.context
    assert "selected_date" not in repository.context
    assert "selected_time" not in repository.context
    assert "candidate_booking" not in repository.context
    assert "quote_presented" not in repository.context


@pytest.mark.asyncio
async def test_side_question_preserves_address_checkpoint() -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_ADDRESS,
        context={"quantity": 2, "request_mode": "booking"},
    )

    await engine.process(inbound(2040, body="antes, quanto custa o serviço?"))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["quantity"] == 2
    assert "service_address" not in repository.context
    assert "pending_service_address" not in repository.context
    assert "endereço" in _last_body(repository)


@pytest.mark.asyncio
async def test_property_side_question_does_not_replace_existing_property_type() -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_DATE,
        context={
            "property_type": "house",
            "service_address": SERVICE_ADDRESS,
            "request_mode": "booking",
        },
    )

    await engine.process(inbound(2050, body="vocês atendem apartamento também?"))

    assert repository.context["property_type"] == "house"
    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("body", "start", "end"),
    [
        ("Portaria libera entre 9 e 18 horas", "09:00", "18:00"),
        ("das 8h às 17h", "08:00", "17:00"),
        ("pode entrar entre 09:00 e 17:30", "09:00", "17:30"),
        ("horário permitido é das 9 às 16", "09:00", "16:00"),
    ],
    ids=["between-hours", "from-to", "between-minutes", "allowed-hours"],
)
@pytest.mark.asyncio
async def test_building_hours_accept_only_explicit_property_windows(
    body: str,
    start: str,
    end: str,
) -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_BUILDING_HOURS,
        context={
            "property_type": "building",
            "request_mode": "quote",
            "service_address": SERVICE_ADDRESS,
        },
    )

    await engine.process(inbound(2060, body=body))

    assert repository.context["building_hours_start"] == start
    assert repository.context["building_hours_end"] == end
    assert repository.context["site_allowed_end"] == end
    assert repository.state == ConversationState.BOOKING_GATE_DETAILS


@pytest.mark.parametrize(
    "body",
    ["horário comercial", "durante o dia", "quando a portaria deixar"],
    ids=["commercial-hours", "daytime", "invalid-expression"],
)
@pytest.mark.asyncio
async def test_building_hours_without_explicit_window_do_not_invent_hours(
    body: str,
) -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_BUILDING_HOURS,
        context={
            "property_type": "building",
            "request_mode": "quote",
            "service_address": SERVICE_ADDRESS,
        },
    )

    await engine.process(inbound(2070, body=body))

    assert repository.state == ConversationState.BOOKING_BUILDING_HOURS
    assert "building_hours_start" not in repository.context
    assert "building_hours_end" not in repository.context
    assert "site_allowed_end" not in repository.context
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("condomínio permite serviço até 17h", "17:00"),
        ("o limite é 18:00", "18:00"),
        ("não pode depois das 16h", "16:00"),
        ("serviço só até 15h", "15:00"),
    ],
    ids=["until-17", "limit-18", "not-after-16", "service-until-15"],
)
@pytest.mark.asyncio
async def test_site_limit_accepts_only_unambiguous_property_restriction(
    body: str,
    expected: str,
) -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_SITE_LIMIT,
        context={"service_address": SERVICE_ADDRESS},
        site_limit=True,
    )

    await engine.process(inbound(2080, body=body))

    assert repository.context["site_limit_answered"] is True
    assert repository.context["site_allowed_end"] == expected
    assert repository.state == ConversationState.BOOKING_DATE


@pytest.mark.parametrize(
    "body",
    [
        "quero às 15h",
        "tem horário às 16h?",
        "o técnico chega 14h?",
        "o endereço é Rua X, número 25",
    ],
    ids=["desired-time", "availability-question", "arrival-question", "address-number"],
)
@pytest.mark.asyncio
async def test_appointment_time_or_address_number_never_becomes_site_limit(
    body: str,
) -> None:
    repository, engine = _scenario(
        ConversationState.BOOKING_SITE_LIMIT,
        context={"service_address": SERVICE_ADDRESS},
        site_limit=True,
    )

    await engine.process(inbound(2090, body=body))

    assert repository.state == ConversationState.BOOKING_SITE_LIMIT
    assert "site_limit_answered" not in repository.context
    assert "site_allowed_end" not in repository.context
    assert repository.automation_enabled is True

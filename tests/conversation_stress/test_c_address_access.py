"""Discovery corpus C: address, property and physical-access checkpoints.

This suite intentionally expresses product invariants rather than mirroring the
current implementation. During discovery, a failing assertion is a reproducible
gap and must not be "fixed" by weakening the expectation.
"""

from dataclasses import replace

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


def _scenario(
    state: str,
    *,
    context: dict | None = None,
    difficult_access: bool = False,
    site_limit: bool = False,
) -> tuple[FakeConversationRepository, FakeBookingPort, ConversationEngine]:
    repository = FakeConversationRepository(
        state=state,
        context={"service_id": str(SERVICE_ID), **(context or {})},
        customer_name="Cliente Teste",
    )
    booking = FakeBookingPort()
    booking.intake = replace(
        booking.intake,
        requires_address=True,
        considers_difficult_access=difficult_access,
        asks_site_time_limit=site_limit,
        pricing_type=PricingType.ESTIMATED,
    )
    booking.business_city = "São José dos Campos"
    booking.business_state = "SP"
    return repository, booking, ConversationEngine(repository, booking)


def _last_body(repository: FakeConversationRepository) -> str:
    return (repository.outbounds[-1].transition.outbound.body or "").casefold()


@pytest.mark.parametrize(
    ("body", "city", "state"),
    [
        ("Rua das Flores, 10, Centro, São José dos Campos - SP", "São José dos Campos", "SP"),
        ("R. das Flores 10, Centro, SJC", "São José dos Campos", "SP"),
        ("Av Brasil, 250, Jardim América, São José dos Campos", "São José dos Campos", "SP"),
        ("Travessa Um 7, Vila Nova, Jacareí - SP", "Jacareí", "SP"),
        ("Rua Projetada 42, Centro, Caçapava SP", "Caçapava", "SP"),
        ("Alameda dos Ipês, número 88, Urbanova, S. J. dos Campos", "São José dos Campos", "SP"),
        ("Estrada Municipal 1200, bairro do Limoeiro, sjc", "São José dos Campos", "SP"),
        ("Rua Quinze de Novembro, nº 31, Centro, Taubaté/SP", "Taubaté", "SP"),
    ],
    ids=[
        "canonical",
        "abbreviated-street-city",
        "avenue-without-state",
        "different-city",
        "city-state-without-dash",
        "dotted-city-alias",
        "lowercase-alias",
        "number-symbol-and-slash",
    ],
)
@pytest.mark.asyncio
async def test_complete_realistic_addresses_advance_without_inventing_locality(
    body: str, city: str, state: str
) -> None:
    repository, _, engine = _scenario(ConversationState.BOOKING_ADDRESS)

    await engine.process(inbound(100, body=body))

    assert repository.state != ConversationState.BOOKING_ADDRESS
    assert repository.state != ConversationState.HUMAN_HANDOFF
    address = repository.context["service_address"]
    assert address["city"].casefold() == city.casefold()
    assert address["state"].casefold() == state.casefold()


@pytest.mark.parametrize(
    "body",
    [
        "Rua das Flores, 10, Centro",
        "R 28, 47, Parque Imperial SP",
        "Av. Andrômeda 500, Jardim Satélite",
        "Estrada do Cajuru km 4, bairro Cajuru",
    ],
    ids=["no-city", "state-is-not-city", "abbreviated-no-city", "rural-no-city"],
)
@pytest.mark.asyncio
async def test_incomplete_address_requests_only_city_and_preserves_pending_address(
    body: str,
) -> None:
    repository, _, engine = _scenario(ConversationState.BOOKING_ADDRESS)

    await engine.process(inbound(110, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["awaiting_address_city"] is True
    assert repository.context["pending_service_address"]
    assert "service_address" not in repository.context
    assert "cidade" in _last_body(repository)


@pytest.mark.parametrize(
    "body",
    [
        "Antes de passar o endereço, quanto custa esse serviço?",
        "rapidinho, quanto tempo demora?",
        "o valor inclui material e deslocamento?",
        "precisa desligar a energia para fazer o serviço?",
        "o técnico leva escada ou eu preciso ter uma?",
        "vocês atendem sábado também?",
    ],
    ids=["price", "duration", "included-items", "technical", "ladder", "availability"],
)
@pytest.mark.asyncio
async def test_side_question_never_becomes_address_and_resumes_address_checkpoint(
    body: str,
) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_ADDRESS,
        context={"quantity": 2, "request_mode": "booking"},
    )

    await engine.process(inbound(120, body=body))

    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["quantity"] == 2
    assert "service_address" not in repository.context
    assert "pending_service_address" not in repository.context
    assert "endereço" in _last_body(repository)
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("state", "body", "forbidden_key", "resume_word"),
    [
        (ConversationState.BOOKING_ACCESS, "quanto fica o serviço?", "access_condition", "acesso"),
        (ConversationState.BOOKING_ACCESS, "demora quantas horas?", "access_condition", "acesso"),
        (ConversationState.BOOKING_PROPERTY, "o orçamento é gratuito?", "property_type", "casa"),
        (ConversationState.BOOKING_PROPERTY, "quanto tempo leva a instalação?", "property_type", "casa"),
        (ConversationState.BOOKING_BUILDING_HOURS, "o técnico leva o material?", "building_hours_start", "horário"),
        (ConversationState.BOOKING_GATE_DETAILS, "qual a duração do serviço?", "gate_instructions", "portaria"),
        (ConversationState.BOOKING_SITE_LIMIT, "precisa de alguém no local?", "site_limit_answered", "horário"),
        (ConversationState.BOOKING_SITE_LIMIT, "tem taxa de visita?", "site_limit_answered", "horário"),
    ],
    ids=[
        "access-price",
        "access-duration",
        "property-price",
        "property-duration",
        "building-hours-technical",
        "gate-duration",
        "site-limit-presence",
        "site-limit-price",
    ],
)
@pytest.mark.asyncio
async def test_side_questions_preserve_physical_checkpoint_and_prior_context(
    state: str, body: str, forbidden_key: str, resume_word: str
) -> None:
    repository, _, engine = _scenario(
        state,
        context={
            "quantity": 2,
            "request_mode": "quote",
            "service_address": SERVICE_ADDRESS,
        },
        difficult_access=True,
        site_limit=True,
    )

    await engine.process(inbound(130, body=body))

    assert repository.state == state
    assert repository.context["service_id"] == str(SERVICE_ID)
    assert repository.context["quantity"] == 2
    assert forbidden_key not in repository.context
    assert resume_word in _last_body(repository)
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("É uma casa térrea", "house"),
        ("residencia", "house"),
        ("É apartamento no terceiro andar", "building"),
        ("fica num prédio comercial", "building"),
        ("condominio fechado", "condominium"),
    ],
    ids=["house", "house-no-accent", "apartment", "commercial-building", "condominium"],
)
@pytest.mark.asyncio
async def test_property_type_is_consumed_only_from_an_actual_property_answer(
    body: str, expected: str
) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_PROPERTY,
        context={"request_mode": "quote", "service_address": SERVICE_ADDRESS},
    )

    await engine.process(inbound(140, body=body))

    assert repository.context["property_type"] == expected
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("acesso normal", "normal"),
        ("é fácil de chegar", "normal"),
        ("tranquilo, sem dificuldade", "normal"),
        ("tem escada e o local é apertado", "difficult"),
        ("o acesso é difícil", "difficult"),
        ("não tenho certeza", "unknown"),
    ],
    ids=["normal", "easy", "no-difficulty", "stairs", "difficult", "unknown"],
)
@pytest.mark.asyncio
async def test_access_condition_understands_natural_answers_without_handoff(
    body: str, expected: str
) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_ACCESS,
        difficult_access=True,
    )

    await engine.process(inbound(150, body=body))

    assert repository.context["access_condition"] == expected
    assert repository.state == ConversationState.BOOKING_ADDRESS
    assert repository.automation_enabled is True


@pytest.mark.parametrize(
    ("body", "start", "end"),
    [
        ("Das 8h às 17h", "08:00", "17:00"),
        ("pode entrar de 08:30 até 16:30", "08:30", "16:30"),
        ("Portaria libera entre 9 e 18 horas", "09:00", "18:00"),
        ("07:00-15:00", "07:00", "15:00"),
    ],
    ids=["canonical", "minutes", "natural-window", "compact-window"],
)
@pytest.mark.asyncio
async def test_building_hours_store_only_explicit_time_window(
    body: str, start: str, end: str
) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_BUILDING_HOURS,
        context={
            "property_type": "building",
            "request_mode": "quote",
            "service_address": SERVICE_ADDRESS,
        },
    )

    await engine.process(inbound(160, body=body))

    assert repository.context["building_hours_start"] == start
    assert repository.context["building_hours_end"] == end
    assert repository.context["site_allowed_end"] == end
    assert repository.state == ConversationState.BOOKING_GATE_DETAILS


@pytest.mark.parametrize(
    "body",
    [
        "Na portaria, chamar no interfone do bloco B",
        "Avisar o porteiro e subir para o apartamento 42",
        "Sem portaria, a entrada é livre",
        "Falar com Cliente Teste na guarita da torre 2",
    ],
    ids=["intercom", "doorman", "no-doorman", "guardhouse"],
)
@pytest.mark.asyncio
async def test_gate_instructions_accept_only_real_access_directions(body: str) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_GATE_DETAILS,
        context={
            "property_type": "building",
            "request_mode": "quote",
            "service_address": SERVICE_ADDRESS,
            "building_hours_start": "08:00",
            "building_hours_end": "17:00",
        },
    )

    await engine.process(inbound(170, body=body))

    assert repository.context["gate_instructions"] == body
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Não há limite de horário", None),
        ("pode trabalhar até 17h", "17:00"),
        ("o limite é 18:00", "18:00"),
    ],
    ids=["no-limit", "until-17", "until-18"],
)
@pytest.mark.asyncio
async def test_site_limit_records_only_explicit_restriction(
    body: str, expected: str | None
) -> None:
    repository, _, engine = _scenario(
        ConversationState.BOOKING_SITE_LIMIT,
        context={"service_address": SERVICE_ADDRESS},
        site_limit=True,
    )

    await engine.process(inbound(180, body=body))

    assert repository.context["site_limit_answered"] is True
    assert repository.context.get("site_allowed_end") == expected
    assert repository.state == ConversationState.BOOKING_DATE

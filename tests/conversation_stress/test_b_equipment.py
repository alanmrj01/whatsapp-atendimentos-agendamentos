"""Rodada 1 — descoberta de robustez em equipamento, quantidade e perfil.

Este arquivo deliberadamente contém expectativas de produto que podem falhar no
baseline. Falhas reproduzíveis são gaps de descoberta; o runtime não é corrigido
nesta rodada.
"""

from dataclasses import replace

import pytest

from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.ports import BookingOption
from tests.test_conversation_engine import (
    FakeBookingPort,
    FakeConversationRepository,
    SERVICE_ID,
    inbound,
)


def _installation_port(*, requires_quantity: bool = False) -> FakeBookingPort:
    port = FakeBookingPort()
    port.services = [
        BookingOption(str(SERVICE_ID), "Instalação de ar-condicionado split")
    ]
    port.intake = replace(
        port.intake,
        requires_quantity=requires_quantity,
    )
    return port


def _equipment_context(**values):
    return {
        "service_id": str(SERVICE_ID),
        "request_mode": "quote",
        **values,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Já tenho o aparelho", "has_equipment"),
        ("possuo o aparelho", "has_equipment"),
        ("só preciso da instalação", "has_equipment"),
        ("apenas instalar", "has_equipment"),
        ("quero comprar o aparelho", "needs_equipment"),
        ("preciso de um ar", "needs_equipment"),
        ("não tenho aparelho", "needs_equipment"),
        ("nao tenho o ar", "needs_equipment"),
    ],
)
async def test_equipment_ownership_consumes_only_an_explicit_answer(
    body: str,
    expected: str,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        context=_equipment_context(),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(100, body=body)
    )

    assert repository.context["equipment_ownership"] == expected
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "Tenho uma dúvida: quanto custa?",
        "Não tenho certeza se cabe no quarto",
        "Esse aparelho tem Wi-Fi?",
        "Quanto tempo demora a instalação?",
        "Hoje tenho só quinze minutos para conversar",
        "Vocês têm garantia?",
    ],
)
async def test_side_question_does_not_become_equipment_ownership(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_OWNERSHIP,
        context=_equipment_context(),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(101, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_OWNERSHIP
    assert "equipment_ownership" not in repository.context
    assert repository.context["service_id"] == str(SERVICE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "LG Dual Inverter 12000 BTU",
        "Samsung WindFree 9000",
        "Midea Xtreme Save 12 mil BTUs",
        "é um Springer 18.000",
        "Electrolux Color Adapt",
        "Consul inverter 9000 btu",
    ],
)
async def test_existing_equipment_model_accepts_plausible_identification(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_MODEL,
        context=_equipment_context(
            equipment_ownership="has_equipment",
            equipment_model_known=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(102, body=body)
    )

    assert repository.context["equipment_model"] == body
    assert repository.state != ConversationState.BOOKING_EQUIPMENT_MODEL
    assert repository.state != ConversationState.HUMAN_HANDOFF


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa esse serviço?",
        "Demora 2 horas para instalar?",
        "Vocês dão garantia de 1 ano?",
        "Atendem no sábado de manhã?",
        "Tenho 2 aparelhos no imóvel",
        "O quarto mede 20 m2",
        "Qual modelo gasta menos energia?",
        "Esse serviço inclui a tubulação?",
    ],
)
async def test_side_question_does_not_become_equipment_model(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_MODEL,
        context=_equipment_context(
            equipment_ownership="has_equipment",
            equipment_model_known=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(103, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_MODEL
    assert "equipment_model" not in repository.context
    assert repository.context["service_id"] == str(SERVICE_ID)
    response = repository.outbounds[-1].transition.outbound.body or ""
    assert "modelo" in response.casefold() or "marca" in response.casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("1", 1),
        ("dois", 2),
        ("São 3 aparelhos", 3),
        ("quatro unidades", 4),
        ("preciso limpar uma unidade", 1),
        ("tenho cinco aparelhos", 5),
    ],
)
async def test_quantity_accepts_only_clear_equipment_count(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context=_equipment_context(),
    )

    await ConversationEngine(
        repository,
        _installation_port(requires_quantity=True),
    ).process(inbound(104, body=body))

    assert repository.context["quantity"] == expected
    assert repository.context["equipment_quantity"] == expected
    assert repository.state != ConversationState.BOOKING_QUANTITY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa? Meu limite é 500 reais",
        "Demora 2 horas?",
        "O modelo é de 12 mil BTUs",
        "A tubulação tem até 3 metros",
        "O endereço é Rua Central, número 25",
        "No ambiente ficam 4 pessoas",
        "A garantia é de 5 anos?",
        "Posso pagar em 10 vezes?",
    ],
)
async def test_incidental_number_does_not_become_equipment_quantity(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_QUANTITY,
        context=_equipment_context(),
    )

    await ConversationEngine(
        repository,
        _installation_port(requires_quantity=True),
    ).process(inbound(105, body=body))

    assert repository.state == ConversationState.BOOKING_QUANTITY
    assert "quantity" not in repository.context
    assert "equipment_quantity" not in repository.context
    assert repository.context["service_id"] == str(SERVICE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("4 pessoas", 4),
        ("no máximo três pessoas", 3),
        ("somos 2", 2),
        ("uma pessoa", 1),
    ],
)
async def test_profile_people_checkpoint_accepts_explicit_occupancy(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(106, body=body)
    )

    assert repository.context["room_people_max"] == expected
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa? Posso gastar até 2 mil reais",
        "A instalação demora 3 horas?",
        "Meu aparelho atual é de 12 mil BTUs",
        "A garantia é de 5 anos?",
        "O endereço é número 40",
    ],
)
async def test_incidental_number_does_not_become_room_occupancy(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(107, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "room_people_max" not in repository.context
    assert repository.context["service_id"] == str(SERVICE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("20 m2", 20),
        ("o quarto tem 16 metros quadrados", 16),
        ("aproximadamente 25m²", 25),
    ],
)
async def test_profile_area_checkpoint_accepts_explicit_room_area(
    body: str,
    expected: int,
) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
            room_people_max=3,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(108, body=body)
    )

    assert repository.context["room_area_m2"] == expected
    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "O aparelho custa 100 reais por mês?",
        "A visita demora 2 horas?",
        "Quero parcelar em 12 vezes",
    ],
)
async def test_incidental_number_does_not_become_room_area(body: str) -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_EQUIPMENT_PROFILE,
        context=_equipment_context(
            equipment_ownership="needs_equipment",
            equipment_model_known=False,
            equipment_profile_intro_sent=True,
            room_people_max=3,
        ),
    )

    await ConversationEngine(repository, _installation_port()).process(
        inbound(109, body=body)
    )

    assert repository.state == ConversationState.BOOKING_EQUIPMENT_PROFILE
    assert "room_area_m2" not in repository.context
    assert repository.context["room_people_max"] == 3

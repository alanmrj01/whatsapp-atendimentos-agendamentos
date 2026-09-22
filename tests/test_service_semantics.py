from app.conversations.interpreter import DeterministicConversationInterpreter
from app.conversations.ports import BookingOption
from app.conversations.service_semantics import (
    generate_service_intent_examples,
    semantic_service_score,
)
from app.conversations.transitions import _service_for_interpretation


def test_service_examples_are_generated_automatically_and_are_varied() -> None:
    examples = generate_service_intent_examples("Limpeza de ar-condicionado")
    assert len(examples) >= 12
    normalized = " ".join(examples).casefold()
    assert "agendar" in normalized
    assert "quanto custa" in normalized
    assert "higien" in normalized


def test_semantic_matching_understands_natural_cleaning_requests() -> None:
    service = BookingOption(
        "1",
        "Limpeza e higienização",
        generate_service_intent_examples("Limpeza e higienização"),
    )
    other = BookingOption(
        "2",
        "Instalação de ar-condicionado",
        generate_service_intent_examples("Instalação de ar-condicionado"),
    )
    for phrase in (
        "quero fazer uma limpeza no ar condicionado",
        "preciso limpar meu ar",
        "quanto custa uma higienização",
        "queria agendar para lavar o split",
        "meu aparelho está sujo e queria higienizar",
    ):
        interpretation = DeterministicConversationInterpreter().interpret(phrase)
        assert _service_for_interpretation((service, other), interpretation) == service


def test_semantic_score_keeps_unrelated_requests_low() -> None:
    score = semantic_service_score(
        "quero trocar uma torneira",
        "Limpeza de ar-condicionado",
        generate_service_intent_examples("Limpeza de ar-condicionado"),
    )
    assert score < 0.48

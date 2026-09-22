import pytest

from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
    normalize_portuguese,
    service_match_score,
)


@pytest.mark.parametrize(
    ("body", "intent", "service_key"),
    [
        ("  BOM   dia! ", ConversationIntent.GREETING, None),
        ("Quero agendar", ConversationIntent.BOOK, None),
        ("Tem horário amanhã?", ConversationIntent.AVAILABILITY, None),
        ("Quero remarcar", ConversationIntent.RESCHEDULE, None),
        ("Cancela minha visita", ConversationIntent.CANCEL, None),
        ("Quero falar com uma pessoa", ConversationIntent.HUMAN_HANDOFF, None),
        ("Quero limpar meu ar", ConversationIntent.SERVICE_INTENT, "cleaning"),
        (
            "Boa tarde, meu ar não está gelando",
            ConversationIntent.SERVICE_INTENT,
            "diagnostics",
        ),
        ("Preciso colocar gás", ConversationIntent.SERVICE_INTENT, "gas-recharge"),
    ],
)
def test_deterministic_interpreter_understands_core_portuguese_intents(
    body: str,
    intent: ConversationIntent,
    service_key: str | None,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.intent is intent
    assert result.service_key == service_key


def test_normalization_removes_accents_case_and_repeated_spacing() -> None:
    assert normalize_portuguese("  INSTALAÇÃO   do Split! ") == "instalacao do split"


def test_aliases_use_word_boundaries_instead_of_substring_guessing() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Preciso guardar um agasalho"
    )

    assert result.intent is ConversationIntent.UNKNOWN


@pytest.mark.parametrize(
    ("message", "label", "examples"),
    [
        (
            "Quero fazer uma limpeza no meu ar condicionado",
            "Higienização de split",
            ("preciso higienizar o ar", "limpeza do ar condicionado"),
        ),
        (
            "Meu split está sujo e queria lavar",
            "Limpeza e higienização",
            ("quero limpar meu ar condicionado",),
        ),
        (
            "Comprei um aparelho e preciso colocar ele na parede",
            "Instalação de ar-condicionado",
            ("quero instalar um ar condicionado",),
        ),
    ],
)
def test_semantic_service_match_understands_natural_customer_phrasing(
    message: str,
    label: str,
    examples: tuple[str, ...],
) -> None:
    assert service_match_score(message, label, examples) >= 0.44


def test_semantic_service_match_does_not_confuse_unrelated_service() -> None:
    cleaning = service_match_score(
        "quero instalar um ar condicionado novo",
        "Limpeza e higienização",
        ("quero limpar meu ar condicionado",),
    )
    installation = service_match_score(
        "quero instalar um ar condicionado novo",
        "Instalação de ar-condicionado",
        ("quero instalar um ar condicionado",),
    )

    assert installation > cleaning
    assert installation >= 0.44

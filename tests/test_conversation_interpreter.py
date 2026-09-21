import pytest

from app.conversations.interpreter import (
    ConversationIntent,
    DeterministicConversationInterpreter,
    normalize_portuguese,
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
            "Eu queria fazer uma limpeza do ar condicionado",
            ConversationIntent.SERVICE_INTENT,
            "cleaning",
        ),
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

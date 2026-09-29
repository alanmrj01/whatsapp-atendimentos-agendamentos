import pytest

from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
    extract_customer_name,
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


def test_compound_greeting_is_understood_without_exact_match() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Olá, bom dia! Tudo bem?"
    )

    assert result.intent is ConversationIntent.GREETING
    assert result.has(ConversationIntent.GREETING)


def test_greeting_and_service_request_are_both_preserved_as_signals() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Bom dia, preciso fazer uma limpeza no meu ar condicionado"
    )

    assert result.intent is ConversationIntent.SERVICE_INTENT
    assert result.has(ConversationIntent.GREETING)
    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key == "cleaning"


def test_interpreter_extracts_explicit_customer_name_without_consuming_request() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Meu nome é Alan e preciso fazer uma limpeza"
    )

    assert result.customer_name == "Alan"
    assert result.intent is ConversationIntent.SERVICE_INTENT


@pytest.mark.parametrize(
    ("body", "intent"),
    [
        ("Quanto custa a limpeza?", ConversationIntent.PRICE_QUESTION),
        ("Quanto tempo demora?", ConversationIntent.DURATION_QUESTION),
        ("O que inclui a limpeza?", ConversationIntent.SERVICE_INTENT),
    ],
)
def test_interpreter_preserves_service_questions_as_secondary_signals(
    body: str,
    intent: ConversationIntent,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(intent)
    if "inclui" in normalize_portuguese(body):
        assert result.has(ConversationIntent.SERVICE_QUESTION)


def test_bare_name_extraction_is_restricted_to_name_like_text() -> None:
    assert extract_customer_name("Alan de Magalhães", allow_bare=True) == (
        "Alan de Magalhães"
    )
    assert extract_customer_name("preciso de uma limpeza", allow_bare=True) is None



@pytest.mark.parametrize(
    "body",
    [
        "gostaria de comprar um ar condicionado",
        "bom dia! gostaria de comprar um ar condiciionado para o meu quarto",
        "vocês vendem aparelho de ar condicionado?",
    ],
)
def test_equipment_purchase_is_distinguished_from_installation(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.intent is ConversationIntent.EQUIPMENT_PURCHASE
    assert result.has(ConversationIntent.EQUIPMENT_PURCHASE)


def test_social_reply_is_not_accepted_as_bare_customer_name() -> None:
    assert extract_customer_name("suave", allow_bare=True) is None
    assert extract_customer_name("beleza", allow_bare=True) is None


@pytest.mark.parametrize(
    ("body", "forbidden"),
    [
        ("Não quero cancelar", ConversationIntent.CANCEL),
        ("Não quero cancelamento", ConversationIntent.CANCEL),
        ("Não quero remarcar", ConversationIntent.RESCHEDULE),
        ("Não quero reagendamento", ConversationIntent.RESCHEDULE),
        ("Não quero falar com atendente", ConversationIntent.HUMAN_HANDOFF),
        ("Não quero falar com uma pessoa", ConversationIntent.HUMAN_HANDOFF),
        ("Não quero falar com alguém", ConversationIntent.HUMAN_HANDOFF),
        ("Não quero um atendente", ConversationIntent.HUMAN_HANDOFF),
    ],
)
def test_negated_actions_do_not_trigger_destructive_intents(
    body: str,
    forbidden: ConversationIntent,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert not result.has(forbidden)
    assert result.has_act(ConversationAct.NEGATED_ACTION)


def test_reschedule_question_is_not_treated_as_action() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Depois posso remarcar?"
    )

    assert result.has(ConversationIntent.RESCHEDULE_QUESTION)
    assert not result.has(ConversationIntent.RESCHEDULE)
    assert result.has_act(ConversationAct.SIDE_QUESTION)


@pytest.mark.parametrize(
    ("body", "service_key"),
    [
        ("Não é limpeza, é manutenção", "diagnostics"),
        ("Não quero comprar, só instalar", "split-installation"),
    ],
)
def test_latest_corrected_service_assertion_wins(
    body: str,
    service_key: str,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.service_key == service_key
    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.has_act(ConversationAct.CORRECTION)


def test_additional_request_is_explicitly_classified() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Também quero uma limpeza"
    )

    assert result.has_act(ConversationAct.ADDITIONAL_REQUEST)
    assert result.service_key == "cleaning"


def test_compound_greeting_only_is_social() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Bom dia, tudo bem?"
    )

    assert result.has(ConversationIntent.GREETING)
    assert result.has_act(ConversationAct.SOCIAL)


def test_discourse_marker_is_social_without_consuming_a_slot() -> None:
    result = DeterministicConversationInterpreter().interpret("Só uma dúvida")

    assert result.has_act(ConversationAct.SOCIAL)


@pytest.mark.parametrize(
    ("body", "equipment_budget", "service_budget", "total_budget"),
    [
        ("Só posso gastar até R$ 1.500 no aparelho", 1500.0, None, None),
        ("Quero um ar-condicionado até 2 mil", 2000.0, None, None),
        ("Meu orçamento para instalação é R$ 800", None, 800.0, None),
        (
            "Tenho orçamento total de R$ 4.000 com instalação",
            None,
            None,
            4000.0,
        ),
    ],
)
def test_interpreter_extracts_budget_scope(
    body: str,
    equipment_budget: float | None,
    service_budget: float | None,
    total_budget: float | None,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)
    assert result.equipment_budget_max == equipment_budget
    assert result.service_budget_max == service_budget
    assert result.total_budget_max == total_budget


def test_cheaper_equipment_question_is_a_price_side_question() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Tem algum modelo mais em conta?"
    )

    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)

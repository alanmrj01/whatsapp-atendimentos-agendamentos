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
        "vocês fazem higienização?",
        "queria lavar meu split",
    ):
        interpretation = DeterministicConversationInterpreter().interpret(phrase)
        assert _service_for_interpretation((service, other), interpretation) == service


def test_semantic_matching_covers_installation_and_maintenance_language() -> None:
    installation = BookingOption(
        "1",
        "Instalação de ar-condicionado split",
        generate_service_intent_examples("Instalação de ar-condicionado split"),
    )
    maintenance = BookingOption(
        "2",
        "Diagnóstico e manutenção corretiva",
        generate_service_intent_examples("Diagnóstico e manutenção corretiva"),
    )
    services = (installation, maintenance)
    cases = {
        installation: (
            "quero instalar um ar",
            "preciso colocar um split",
            "quanto custa instalar?",
            "quero instalar 3 aparelhos",
            "a condensadora fica alta",
        ),
        maintenance: (
            "meu ar não está gelando",
            "o aparelho parou",
            "preciso de manutenção",
            "está pingando água",
            "está fazendo barulho",
        ),
    }

    interpreter = DeterministicConversationInterpreter()
    for expected, phrases in cases.items():
        for phrase in phrases:
            assert _service_for_interpretation(
                services, interpreter.interpret(phrase)
            ) == expected


def test_semantic_matching_understands_gas_recharge_without_confusing_unrelated() -> None:
    recharge = BookingOption(
        "1",
        "Recarga de gás e teste de vazamento",
        generate_service_intent_examples("Recarga de gás e teste de vazamento"),
    )
    cleaning = BookingOption(
        "2",
        "Limpeza de ar-condicionado",
        generate_service_intent_examples("Limpeza de ar-condicionado"),
    )
    interpreter = DeterministicConversationInterpreter()

    for phrase in ("preciso fazer uma recarga de gás", "meu ar está sem gás"):
        assert _service_for_interpretation(
            (recharge, cleaning), interpreter.interpret(phrase)
        ) == recharge
    for phrase in (
        "quero trocar uma torneira",
        "preciso pintar a sala",
        "vocês consertam geladeira?",
    ):
        assert _service_for_interpretation(
            (recharge, cleaning), interpreter.interpret(phrase)
        ) is None


def test_semantic_ambiguity_does_not_choose_an_arbitrary_service() -> None:
    first = BookingOption(
        "1",
        "Limpeza residencial",
        generate_service_intent_examples("Limpeza residencial"),
    )
    second = BookingOption(
        "2",
        "Limpeza comercial",
        generate_service_intent_examples("Limpeza comercial"),
    )

    assert _service_for_interpretation(
        (first, second),
        DeterministicConversationInterpreter().interpret("quero uma limpeza"),
    ) is None


def test_semantic_score_keeps_unrelated_requests_low() -> None:
    score = semantic_service_score(
        "quero trocar uma torneira",
        "Limpeza de ar-condicionado",
        generate_service_intent_examples("Limpeza de ar-condicionado"),
    )
    assert score < 0.48

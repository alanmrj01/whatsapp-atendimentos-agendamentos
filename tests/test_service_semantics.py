from app.conversations.interpreter import DeterministicConversationInterpreter
from app.conversations.ports import BookingOption
from app.conversations.service_semantics import (
    generate_service_intent_examples,
    normalize_service_text,
    semantic_service_score,
)
from app.conversations.transitions import (
    _service_candidates_for_interpretation,
    _service_for_interpretation,
)


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
            "vocês fazem manutenção de ar-condicionado?",
            "faz manutenção em ar condicionado?",
            "trabalham com conserto de split?",
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


def test_semantic_matching_normalizes_common_btu_shorthand() -> None:
    assert "capacidade_9000" in normalize_service_text("instalar ar condicionado 9 BTU")
    assert "capacidade_9000" in normalize_service_text("instalação split 9 mil BTUs")
    assert "capacidade_9000" in normalize_service_text("instalação split 9000 BTU")


def test_dynamic_catalog_resolves_installation_budget_without_equipment_purchase_noise() -> None:
    installation = BookingOption(
        "1",
        "Instalar ar condicionado",
        generate_service_intent_examples("Instalar ar condicionado"),
    )
    cleaning = BookingOption(
        "2",
        "Limpeza e higienização",
        generate_service_intent_examples("Limpeza e higienização"),
    )
    maintenance = BookingOption(
        "3",
        "Manutenção preventiva",
        generate_service_intent_examples("Manutenção preventiva"),
    )
    interpretation = DeterministicConversationInterpreter().interpret(
        "Bom dia, gostaria de fazer um orçamento de uma instalação"
    )

    assert _service_for_interpretation(
        (installation, cleaning, maintenance),
        interpretation,
    ) == installation


def test_dynamic_catalog_uses_capacity_to_resolve_specific_installation() -> None:
    installation_9 = BookingOption(
        "1",
        "Instalação de ar condicionado split 9 mil BTUs",
        generate_service_intent_examples("Instalação de ar condicionado split 9 mil BTUs"),
    )
    installation_12 = BookingOption(
        "2",
        "Instalação de ar condicionado split 12 mil BTUs",
        generate_service_intent_examples("Instalação de ar condicionado split 12 mil BTUs"),
    )
    cleaning = BookingOption(
        "3",
        "Limpeza e higienização",
        generate_service_intent_examples("Limpeza e higienização"),
    )
    interpretation = DeterministicConversationInterpreter().interpret(
        "instalar ar condicionado 9 btu na sala de estar"
    )

    assert _service_for_interpretation(
        (installation_9, installation_12, cleaning),
        interpretation,
    ) == installation_9


def test_generic_installation_ambiguity_filters_only_installation_family() -> None:
    installation_9 = BookingOption(
        "1",
        "Instalação de ar condicionado split 9 mil BTUs",
        generate_service_intent_examples("Instalação de ar condicionado split 9 mil BTUs"),
    )
    installation_12 = BookingOption(
        "2",
        "Instalação de ar condicionado split 12 mil BTUs",
        generate_service_intent_examples("Instalação de ar condicionado split 12 mil BTUs"),
    )
    cleaning = BookingOption(
        "3",
        "Limpeza e higienização",
        generate_service_intent_examples("Limpeza e higienização"),
    )
    interpretation = DeterministicConversationInterpreter().interpret(
        "gostaria de fazer um orçamento para instalar ar condicionado"
    )

    candidates = _service_candidates_for_interpretation(
        (installation_9, installation_12, cleaning),
        interpretation,
    )

    assert set(candidates) == {installation_9, installation_12}
    assert _service_for_interpretation(
        (installation_9, installation_12, cleaning),
        interpretation,
    ) is None


def test_semantic_matching_covers_preventive_corrective_and_pmoc_catalog_names() -> None:
    preventive = BookingOption(
        "1",
        "Manutenção preventiva",
        generate_service_intent_examples("Manutenção preventiva"),
    )
    corrective = BookingOption(
        "2",
        "Manutenção corretiva",
        generate_service_intent_examples("Manutenção corretiva"),
    )
    pmoc = BookingOption(
        "3",
        "PMOC - Plano de manutenção",
        generate_service_intent_examples("PMOC - Plano de manutenção"),
    )
    services = (preventive, corrective, pmoc)
    interpreter = DeterministicConversationInterpreter()

    assert _service_for_interpretation(
        services,
        interpreter.interpret("quero fazer uma preventiva no ar"),
    ) == preventive
    assert _service_for_interpretation(
        services,
        interpreter.interpret("preciso de manutenção corretiva"),
    ) == corrective
    assert _service_for_interpretation(
        services,
        interpreter.interpret("preciso de orçamento para pmoc"),
    ) == pmoc

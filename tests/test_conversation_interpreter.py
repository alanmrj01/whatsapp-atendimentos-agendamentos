import pytest

from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    DeterministicConversationInterpreter,
    extract_customer_name,
    normalize_portuguese,
    normalize_profile_customer_name,
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


@pytest.mark.parametrize(
    "body",
    [
        "Quanto custa um ar condicionado?",
        "Qual o valor de um aparelho?",
        "Gostaria de uma cotação de um ar condicionado",
    ],
)
def test_generic_equipment_price_request_enters_purchase_flow(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.EQUIPMENT_PURCHASE)
    assert result.has(ConversationIntent.PRICE_QUESTION)


@pytest.mark.parametrize(
    "body",
    [
        "Gostaria de orçar um ar condicionado",
        "Quero orçar um ar condicionado",
        "Pode me orçar um split?",
        "Boa noite, gostaria de orçar um ar condicionado",
    ],
)
def test_equipment_quote_verb_enters_purchase_flow(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.intent is ConversationIntent.EQUIPMENT_PURCHASE
    assert result.has(ConversationIntent.EQUIPMENT_PURCHASE)
    assert result.has(ConversationIntent.PRICE_QUESTION)


def test_service_quote_with_orcar_stays_in_service_flow() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Gostaria de orçar a instalação de ar condicionado"
    )

    assert result.intent is ConversationIntent.SERVICE_INTENT
    assert result.service_key == "split-installation"
    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert not result.has(ConversationIntent.EQUIPMENT_PURCHASE)


def test_service_price_question_is_not_stolen_by_equipment_purchase() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Quanto custa a manutenção do ar condicionado?"
    )

    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key == "diagnostics"
    assert not result.has(ConversationIntent.EQUIPMENT_PURCHASE)


def test_physical_limit_is_not_parsed_as_money_budget() -> None:
    result = DeterministicConversationInterpreter().interpret("Até 3 metros")

    assert result.equipment_budget_max is None
    assert result.service_budget_max is None
    assert result.total_budget_max is None
    assert not result.has(ConversationIntent.PRICE_QUESTION)


def test_currency_budget_still_parses_after_physical_limit_guard() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Só posso gastar até R$ 1.500 no aparelho"
    )

    assert result.equipment_budget_max == 1500.0
    assert result.has(ConversationIntent.PRICE_QUESTION)


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


def test_direct_need_for_equipment_with_budget_is_purchase_intent() -> None:
    result = DeterministicConversationInterpreter().interpret(
        "Eu preciso de um ar-condicionado, mas só posso gastar até R$ 1.500"
    )

    assert result.has(ConversationIntent.EQUIPMENT_PURCHASE)
    assert result.equipment_budget_max == 1500.0

@pytest.mark.parametrize(
    "body",
    [
        "Tem como melhorar o valor?",
        "Vocês conseguem dar desconto?",
        "Achei muito caro",
        "Tem um preço menor?",
    ],
)
def test_price_objection_language_is_recognized_as_price_question(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)

@pytest.mark.parametrize(
    "body",
    [
        "Meu ar-condicionado está com problema",
        "Estou com problema no ar",
    ],
)
def test_generic_ac_problem_maps_to_corrective_diagnostics(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.service_key == "diagnostics"

@pytest.mark.parametrize(
    "body",
    [
        "Vocês fazem manutenção?",
        "Tem manutenção corretiva?",
        "Vocês trabalham com limpeza?",
    ],
)
def test_service_inquiry_is_side_question_not_implicit_workflow_change(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.SERVICE_INTENT)
    assert result.has(ConversationIntent.SERVICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)

@pytest.mark.parametrize(
    "body",
    [
        "Qual a voltagem desse aparelho?",
        "Esse modelo tem Wi-Fi?",
        "Qual a garantia do serviço?",
        "Qual a pressão exata do gás nesse aparelho?",
        "Quais as medidas da condensadora?",
    ],
)
def test_technical_questions_are_side_questions(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.TECHNICAL_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)

@pytest.mark.parametrize(
    "body",
    [
        # Conversa cotidiana / social.
        "Nossa dormi tão mal",
        "Amor",
        "Chegou bem",
        "Tira isso pfv",
        "Você já chegou",
        "Tudo certo",
        "Pode ser",
        "Tá bom",
        "Deu certo",
        "Eu te aviso",
        "Já estou indo",
        "Cheguei agora",
        "Me manda depois",
        "Acho que sim",
        "Por favor",
        # Perguntas e respostas comuns em atendimento.
        "Qual o valor",
        "Quanto fica",
        "Tem horário amanhã",
        "Pode ser de manhã",
        "Quero agendar",
        "Preciso de ajuda",
        "Agora pode",
        "Não sei",
        "Não lembro",
        # Contexto de refrigeração / local / sintomas.
        "Hoje está muito quente",
        "Está muito quente",
        "Tenho um problema",
        "Quero comprar",
        "Meu ar parou",
        "Não está gelando",
        "Faz muito barulho",
        "Está pingando água",
        "É no quarto",
        "Moro em apartamento",
        "Fica no terceiro andar",
        "Rua das Flores",
        "Meu endereço",
    ],
)
def test_bare_name_extraction_rejects_conversation_fragments(body: str) -> None:
    assert extract_customer_name(body, allow_bare=True) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("alan", "Alan"),
        ("Maria Clara", "Maria Clara"),
        ("João de Souza", "João de Souza"),
        ("Ana Paula Lima", "Ana Paula Lima"),
    ],
)
def test_bare_name_extraction_keeps_real_name_shapes(
    body: str,
    expected: str,
) -> None:
    assert extract_customer_name(body, allow_bare=True) == expected


def test_explicit_name_can_use_word_that_is_ambiguous_in_bare_conversation() -> None:
    assert extract_customer_name("Meu nome é Amor", allow_bare=True) == "Amor"

@pytest.mark.parametrize(
    "profile_name",
    [
        "Nossa dormi tão mal",
        "Chegou bem",
        "Tira isso pfv",
        "Tudo certo",
        "Meu ar parou",
        "Moro em apartamento",
    ],
)
def test_profile_customer_name_rejects_conversation_fragments(profile_name: str) -> None:
    assert normalize_profile_customer_name(profile_name) is None



@pytest.mark.parametrize(
    ("body", "expected_intent", "service_key"),
    [
        ("Quero fazer um orçamento para instalar meu ar", ConversationIntent.SERVICE_INTENT, "split-installation"),
        ("Gostaria de cotar a instalação de um split", ConversationIntent.SERVICE_INTENT, "split-installation"),
        ("Preciso colocar o ar condicionado na sala", ConversationIntent.SERVICE_INTENT, "split-installation"),
        ("Queria montar um ar condicionado no quarto", ConversationIntent.SERVICE_INTENT, "split-installation"),
        ("Preciso higienizar meu ar condicionado", ConversationIntent.SERVICE_INTENT, "cleaning"),
        ("Quero fazer uma lavagem no split", ConversationIntent.SERVICE_INTENT, "cleaning"),
        ("O ar está com mau cheiro e precisa limpar", ConversationIntent.SERVICE_INTENT, "cleaning"),
        ("Quero fazer manutenção preventiva", ConversationIntent.SERVICE_INTENT, "preventive-maintenance"),
        ("Preciso de uma revisão periódica", ConversationIntent.SERVICE_INTENT, "preventive-maintenance"),
        ("Preciso de PMOC", ConversationIntent.SERVICE_INTENT, "pmoc"),
        ("Quero orçamento para plano de manutenção operação e controle", ConversationIntent.SERVICE_INTENT, "pmoc"),
        ("Meu ar não liga", ConversationIntent.SERVICE_INTENT, "diagnostics"),
        ("O aparelho não resfria", ConversationIntent.SERVICE_INTENT, "diagnostics"),
        ("Está gelando pouco", ConversationIntent.SERVICE_INTENT, "diagnostics"),
        ("Preciso de assistência técnica", ConversationIntent.SERVICE_INTENT, "diagnostics"),
        ("Quero reparar o ar condicionado", ConversationIntent.SERVICE_INTENT, "diagnostics"),
        ("Preciso repor gás do ar", ConversationIntent.SERVICE_INTENT, "gas-recharge"),
        ("Quero fazer carga de gás", ConversationIntent.SERVICE_INTENT, "gas-recharge"),
        ("Preciso de teste de vazamento", ConversationIntent.SERVICE_INTENT, "gas-recharge"),
        ("Quero caça vazamento no ar", ConversationIntent.SERVICE_INTENT, "gas-recharge"),
    ],
)
def test_enriched_service_language_maps_to_expected_deterministic_intent(
    body: str,
    expected_intent: ConversationIntent,
    service_key: str,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.intent is expected_intent
    assert result.service_key == service_key
    assert result.has(ConversationIntent.SERVICE_INTENT)


@pytest.mark.parametrize(
    "body",
    [
        "Quanto sai a instalação?",
        "Queria uma estimativa de valor da limpeza",
        "Pode fazer um orçamento da manutenção?",
        "Quero pedir orçamento para o serviço",
        "Quanto fica esse serviço?",
    ],
)
def test_enriched_price_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)


@pytest.mark.parametrize(
    "body",
    [
        "Tem vaga amanhã?",
        "Quais horários vocês têm?",
        "Quando vocês conseguem vir?",
        "Tem agenda disponível essa semana?",
        "Qual horário disponível?",
    ],
)
def test_enriched_availability_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.AVAILABILITY)


@pytest.mark.parametrize(
    "body",
    [
        "Quero marcar uma visita",
        "Preciso marcar atendimento",
        "Pode mandar um técnico?",
        "Preciso de uma visita",
        "Quero agendamento",
    ],
)
def test_enriched_booking_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.BOOK)


@pytest.mark.parametrize(
    "body",
    [
        "Quero alterar o horário",
        "Preciso trocar a data",
        "Quero mudar a data do atendimento",
        "Preciso de remarcação",
    ],
)
def test_enriched_reschedule_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.RESCHEDULE)


@pytest.mark.parametrize(
    "body",
    [
        "Quero desmarcar",
        "Preciso de desmarcação",
        "Pode cancelar minha visita?",
    ],
)
def test_enriched_cancel_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.CANCEL)


@pytest.mark.parametrize(
    "body",
    [
        "Quero falar com humano",
        "Chama um atendente",
        "Quero atendimento humano",
        "Pode chamar atendente?",
    ],
)
def test_enriched_human_handoff_language_is_recognized(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.HUMAN_HANDOFF)


@pytest.mark.parametrize(
    "body",
    [
        "Não quero falar com humano",
        "Não quero chamar atendente",
        "Não quero desmarcar",
        "Não quero trocar a data",
        "Não quero comprar, só instalar",
    ],
)
def test_enriched_lexicon_respects_negation_and_does_not_steal_intent(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    normalized = normalize_portuguese(body)
    if "humano" in normalized or "atendente" in normalized:
        assert not result.has(ConversationIntent.HUMAN_HANDOFF)
    if "desmarcar" in normalized:
        assert not result.has(ConversationIntent.CANCEL)
    if "trocar a data" in normalized:
        assert not result.has(ConversationIntent.RESCHEDULE)
    if "so instalar" in normalized:
        assert result.service_key == "split-installation"
        assert not result.has(ConversationIntent.EQUIPMENT_PURCHASE)


@pytest.mark.parametrize(
    "body",
    [
        "Meu orçamento está apertado esse mês",
        "Tenho uma visita da família amanhã",
        "Vou marcar isso no meu calendário",
        "A revisão do carro ficou cara",
        "Preciso limpar a sala de casa",
        "Meu celular não liga",
        "Vou chamar uma pessoa aqui em casa",
        "Dou muito valor a isso",
        "Vou retirar um pedido depois",
    ],
)
def test_enriched_lexicon_does_not_overclassify_unrelated_conversation(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert not result.has(ConversationIntent.SERVICE_INTENT)
    if body == "Tenho uma visita da família amanhã":
        assert not result.has(ConversationIntent.BOOK)


@pytest.mark.parametrize(
    "body",
    [
        "Quanto você cobra?",
        "Quanto vocês cobram?",
        "Você cobra quanto?",
        "Quanto vai me cobrar?",
        "Quanto eu vou gastar?",
        "Me passa o valor",
    ],
)
def test_natural_charge_language_is_price_question(body: str) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(ConversationIntent.PRICE_QUESTION)
    assert result.has_act(ConversationAct.SIDE_QUESTION)



@pytest.mark.parametrize(
    ("body", "intent", "act"),
    [
        ("Fica em quanto?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Leva o dia todo?", ConversationIntent.DURATION_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Tem encaixe?", ConversationIntent.AVAILABILITY, None),
        ("Manda um técnico", ConversationIntent.BOOK, None),
        ("Desmarca meu horário", ConversationIntent.CANCEL, None),
        ("Como cancela?", ConversationIntent.CANCEL_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Passa pra outro dia", ConversationIntent.RESCHEDULE, None),
        ("Dá pra trocar o horário?", ConversationIntent.RESCHEDULE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Me passa pra um atendente", ConversationIntent.HUMAN_HANDOFF, None),
        ("O que o técnico faz?", ConversationIntent.SERVICE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("É 110 ou 220?", ConversationIntent.TECHNICAL_QUESTION, ConversationAct.SIDE_QUESTION),
    ],
)
def test_round9_language_roots_cover_structural_variants(
    body: str,
    intent: ConversationIntent,
    act: ConversationAct | None,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(intent)
    if act is not None:
        assert result.has_act(act)



@pytest.mark.parametrize(
    ("body", "intent", "act"),
    [
        ("Qual seria o valor?", ConversationIntent.PRICE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Oi, não quero mais o agendamento", ConversationIntent.CANCEL, None),
        ("Se eu desistir como funciona?", ConversationIntent.CANCEL_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Se eu precisar mudar o horário posso?", ConversationIntent.RESCHEDULE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Como funciona pra mudar o dia?", ConversationIntent.RESCHEDULE_QUESTION, ConversationAct.SIDE_QUESTION),
        ("Posso passar pra outro dia?", ConversationIntent.RESCHEDULE_QUESTION, ConversationAct.SIDE_QUESTION),
    ],
)
def test_round9_residual_language_roots(
    body: str,
    intent: ConversationIntent,
    act: ConversationAct | None,
) -> None:
    result = DeterministicConversationInterpreter().interpret(body)

    assert result.has(intent)
    if act is not None:
        assert result.has_act(act)

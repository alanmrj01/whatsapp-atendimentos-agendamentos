from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_GENERIC_TEMPLATES = (
    "quero {service}",
    "queria {service}",
    "preciso de {service}",
    "estou precisando de {service}",
    "gostaria de fazer {service}",
    "vocês fazem {service}",
    "trabalham com {service}",
    "realizam {service}",
    "oferecem {service}",
    "quanto custa {service}",
    "quanto fica {service}",
    "qual o valor de {service}",
    "quero orçamento para {service}",
    "gostaria de um orçamento para {service}",
    "quero cotação de {service}",
    "quero agendar {service}",
    "quero marcar {service}",
    "tem horário para {service}",
    "preciso marcar {service}",
    "preciso de uma visita para {service}",
    "pode fazer {service}",
)

_DOMAIN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "limpeza": (
        "higienização",
        "higienizar",
        "limpar",
        "lavagem",
        "lavar",
        "sanitização",
        "sanitizar",
        "limpeza técnica",
        "sujo",
        "suja",
        "mau cheiro",
        "cheiro ruim",
        "limpeza de ar condicionado",
        "higienizacao de ar condicionado",
        "limpeza de split",
        "higienizacao de split",
    ),
    "higienizacao": ("limpeza", "higienizar", "limpar", "lavagem"),
    "instalacao": (
        "instalar",
        "instalar ar condicionado",
        "colocar",
        "colocar ar condicionado",
        "montagem",
        "montar",
        "montar ar condicionado",
        "fixar",
        "condensadora",
        "evaporadora",
        "fica alta",
        "altura",
    ),
    "desinstalacao": (
        "desinstalar",
        "desinstalar ar condicionado",
        "desinstalar split",
        "retirar o ar condicionado",
        "retirar split",
        "retirar ar condicionado",
        "remover o ar condicionado",
        "remover ar condicionado",
        "remover split",
        "tirar o ar",
        "tirar ar condicionado",
        "remocao do ar condicionado",
    ),
    "realocacao": (
        "realocar",
        "mudar de lugar",
        "trocar de lugar",
        "remanejar",
        "remanejamento",
        "retirar e instalar",
    ),
    "infraestrutura": (
        "infra",
        "pre instalacao",
        "pre-instalacao",
        "preparar ponto",
        "ponto de ar",
        "tubulacao",
        "dreno",
        "infra pronta",
    ),
    "manutencao": (
        "revisão",
        "revisar",
        "conserto",
        "consertar",
        "reparo",
        "reparar",
        "assistência técnica",
        "arrumar",
        "não gela",
        "não está gelando",
        "não resfria",
        "não esfria",
        "gelando pouco",
        "gelando fraco",
        "parou",
        "não liga",
        "não funciona",
        "pingando",
        "vazando água",
        "barulho",
        "ruído",
    ),
    "preventiva": (
        "preventivo",
        "manutenção preventiva",
        "revisão preventiva",
        "revisão periódica",
    ),
    "corretiva": (
        "corretivo",
        "manutenção corretiva",
        "conserto",
        "reparo",
    ),
    "pmoc": (
        "plano de manutenção operação e controle",
        "plano de manutencao operacao e controle",
        "manutenção pmoc",
        "manutencao pmoc",
    ),
    "diagnostico": ("avaliar", "verificar", "descobrir o problema"),
    "gas": (
        "recarga de gás",
        "carga de gás",
        "colocar gás",
        "completar gás",
        "repor gás",
        "sem gás",
    ),
    "vazamento": (
        "teste de vazamento",
        "procurar vazamento",
        "caça vazamento",
        "achar vazamento",
        "vazando gás",
    ),
    "split": ("ar condicionado", "ar-condicionado", "aparelho", "ar"),
    "geladeira": (
        "refrigerador",
        "geladeira nao gela",
        "geladeira nao esta gelando",
        "geladeira gelando pouco",
        "geladeira nao liga",
        "geladeira fazendo barulho",
        "geladeira vazando agua",
        "conserto de geladeira",
        "manutencao de geladeira",
    ),
    "refrigerador": (
        "geladeira",
        "refrigerador nao gela",
        "refrigerador nao liga",
        "conserto de refrigerador",
        "manutencao de refrigerador",
    ),
    "freezer": (
        "congelador",
        "freezer nao congela",
        "freezer nao gela",
        "freezer nao liga",
        "conserto de freezer",
        "manutencao de freezer",
    ),
    "frigobar": (
        "frigobar nao gela",
        "frigobar nao liga",
        "conserto de frigobar",
        "manutencao de frigobar",
    ),
    "cervejeira": (
        "cervejeira nao gela",
        "cervejeira nao liga",
        "conserto de cervejeira",
        "manutencao de cervejeira",
    ),
    "eletrica": (
        "eletricista",
        "eletrica residencial",
        "instalacao eletrica",
        "manutencao eletrica",
        "reparo eletrico",
        "tomada",
        "disjuntor",
        "chuveiro",
        "interruptor",
        "luminaria",
        "fiacao",
        "curto circuito",
    ),
    "eletrico": (
        "eletrica",
        "eletricista",
        "reparo eletrico",
        "instalacao eletrica",
    ),
    "tomada": (
        "tomada sem energia",
        "tomada nao funciona",
        "tomada queimou",
        "trocar tomada",
        "instalar tomada",
    ),
    "disjuntor": (
        "disjuntor desarmando",
        "disjuntor desarma",
        "disjuntor caindo",
        "trocar disjuntor",
    ),
    "chuveiro": (
        "chuveiro nao liga",
        "chuveiro nao esquenta",
        "chuveiro queimou",
        "instalar chuveiro",
        "trocar chuveiro",
    ),
    "interruptor": (
        "interruptor nao funciona",
        "trocar interruptor",
        "instalar interruptor",
    ),
    "luminaria": (
        "luz nao acende",
        "instalar luminaria",
        "trocar luminaria",
    ),
    "fiacao": (
        "fio queimado",
        "fiacao aquecendo",
        "trocar fiacao",
        "refazer fiacao",
    ),
}

_PERSONAL_NON_SERVICE_PHRASES = (
    "revisao do carro",
    "revisao do veiculo",
    "revisao da moto",
    "meu celular nao liga",
    "celular nao liga",
    "telefone nao liga",
    "computador nao liga",
    "notebook nao liga",
    "limpar a sala",
    "limpar sala",
    "limpar o quarto",
    "limpar quarto",
    "limpar a cozinha",
    "limpar cozinha",
    "limpar a casa",
    "vou retirar um pedido",
    "retirar um pedido",
)


_SEMANTIC_STOPWORDS = {
    "a",
    "ao",
     "aparelho",
    "de",
    "do",
    "e",
    "em",
    "esta",
    "fazer",
    "meu",
    "no",
    "o",
    "para",
    "preciso",
    "quero",
    "queria",
    "gostaria",
    "orcamento",
    "cotacao",
    "servico",
     "um",
    "uma",
    "voces",
}


def normalize_service_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())
    normalized = re.sub(
        r"\b(\d{1,2})\s*mil\s*btu?s?\b",
        lambda match: f"capacidade_{int(match.group(1)) * 1000}",
        normalized,
    )
    normalized = re.sub(
        r"\b(\d{4,6})\s*btu?s?\b",
        lambda match: f"capacidade_{int(match.group(1))}",
        normalized,
    )

    def shorthand_capacity(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if 7 <= value <= 60:
            return f"capacidade_{value * 1000}"
        return match.group(0)

    normalized = re.sub(
        r"\b(\d{1,2})\s*btu?s?\b",
        shorthand_capacity,
        normalized,
    )
    return normalized


def generate_service_intent_examples(name: str) -> tuple[str, ...]:
    service = " ".join(name.split())
    normalized = normalize_service_text(service)
    variants: list[str] = []
    variant_keys: set[str] = set()

    def add_variant(value: str) -> None:
        key = normalize_service_text(value)
        if key and key not in variant_keys:
            variant_keys.add(key)
            variants.append(value)

    add_variant(service)
    tokens = set(normalized.split())
    for token, expansions in _DOMAIN_EXPANSIONS.items():
        if token in tokens:
            for expansion in expansions:
                add_variant(expansion)
    if "ar" in tokens or "condicionado" in tokens or "split" in tokens:
        for expansion in ("ar condicionado", "ar-condicionado", "split", "aparelho"):
            add_variant(expansion)

    examples: list[str] = []
    seen: set[str] = set()

    def add_example(sentence: str) -> None:
        key = normalize_service_text(sentence)
        if key and key not in seen:
            seen.add(key)
            examples.append(sentence)

    # Primeiro garante diversidade de atos para o nome configurado:
    # pergunta, orçamento, agendamento, disponibilidade etc.
    for template in _GENERIC_TEMPLATES:
        add_example(template.format(service=service))

    # Depois garante que TODAS as raízes lexicais tenham pelo menos uma
    # formulação natural antes de multiplicar templates.
    for variant in variants[1:]:
        add_example(f"quero {variant}")
        if len(examples) >= 64:
            return tuple(examples)

    for template in (
        "preciso de {service}",
        "vocês fazem {service}",
        "quanto custa {service}",
        "quero agendar {service}",
    ):
        for variant in variants[1:]:
            add_example(template.format(service=variant))
            if len(examples) >= 64:
                return tuple(examples)
    return tuple(examples)


def configured_service_name_matches(message: str, service_name: str) -> bool:
    """Return True only for a unique-worthy multi-token configured name match."""

    query_tokens = set(normalize_service_text(message).split()) - _SEMANTIC_STOPWORDS
    service_tokens = set(normalize_service_text(service_name).split()) - _SEMANTIC_STOPWORDS
    return len(service_tokens) >= 2 and service_tokens.issubset(query_tokens)


def service_domain_compatible(
    message: str,
    service_name: str,
    examples: Iterable[str],
) -> bool:
    """Prevent symptom overlap from crossing an explicit configured domain."""

    query = normalize_service_text(message)
    if not query:
        return True

    cold_appliance_signals = {
        "geladeira",
        "refrigerador",
        "freezer",
        "frigobar",
        "cervejeira",
    }
    electrical_signals = {
        "eletrica",
        "eletricista",
        "tomada",
        "disjuntor",
        "chuveiro",
        "interruptor",
        "luminaria",
        "fiacao",
    }

    query_expanded = _expanded_tokens(query)
    query_tokens = set(query.split())
    explicit_hvac_request = (
        _contains_phrase(query, "ar condicionado")
        or bool(query_tokens & {"split", "condensadora", "evaporadora"})
    )

    required_groups: list[set[str]] = []
    if query_expanded & cold_appliance_signals:
        required_groups.append(cold_appliance_signals)
    if query_expanded & electrical_signals:
        required_groups.append(electrical_signals)
    if not required_groups and not explicit_hvac_request:
        return True

    candidate_expanded = _expanded_tokens(service_name)
    candidate_text = normalize_service_text(service_name)
    for example in examples:
        candidate_expanded.update(_expanded_tokens(example))
        candidate_text += f" {normalize_service_text(example)}"

    candidate_tokens = set(candidate_text.split())
    candidate_has_explicit_hvac = (
        _contains_phrase(candidate_text, "ar condicionado")
        or bool(candidate_tokens & {"split", "condensadora", "evaporadora"})
    )
    if (
        explicit_hvac_request
        and candidate_expanded & (cold_appliance_signals | electrical_signals)
        and not candidate_has_explicit_hvac
    ):
        return False

    return all(
        bool(candidate_expanded & group)
        for group in required_groups
    )


def semantic_service_score(
    message: str,
    service_name: str,
    examples: Iterable[str],
) -> float:
    query = normalize_service_text(message)
    if not query:
        return 0.0
    if not service_domain_compatible(message, service_name, examples):
        return 0.0
    personal_domain_tokens = {
        "carro",
        "veiculo",
        "moto",
        "celular",
        "telefone",
        "computador",
        "notebook",
        "chuveiro",
        "geladeira",
        "refrigerador",
        "freezer",
        "frigobar",
        "cervejeira",
        "tomada",
        "disjuntor",
        "interruptor",
        "luminaria",
    }
    query_tokens = set(query.split())
    mentioned_personal_domains = query_tokens & personal_domain_tokens
    if mentioned_personal_domains:
        candidate_domain_text = " ".join(
            (
                normalize_service_text(service_name),
                *(
                    normalize_service_text(example)
                    for example in examples
                    if isinstance(example, str)
                ),
            )
        )
        candidate_domain_tokens = set(candidate_domain_text.split())
        if mentioned_personal_domains.isdisjoint(candidate_domain_tokens):
            return 0.0

    query_capacities = {
        token for token in query.split() if token.startswith("capacidade_")
    }
    service_text = normalize_service_text(service_name)
    service_capacities = {
        token for token in service_text.split() if token.startswith("capacidade_")
    }
    if query_capacities and service_capacities:
        if query_capacities.isdisjoint(service_capacities):
            return 0.0
        capacity_bonus = 0.22
    else:
        capacity_bonus = 0.0

    candidates = (service_name, *tuple(examples))
    base_score = max(
        (_semantic_similarity(query, value) for value in candidates),
        default=0.0,
    )

    domain_signals = {
        "geladeira",
        "refrigerador",
        "freezer",
        "frigobar",
        "cervejeira",
        "tomada",
        "disjuntor",
        "chuveiro",
        "interruptor",
        "luminaria",
        "fiacao",
        "eletricista",
    }
    query_expanded = _expanded_tokens(query)
    candidate_expanded = _expanded_tokens(service_name)
    for example in examples:
        candidate_expanded.update(_expanded_tokens(example))
    shared_domain = (
        query_expanded
        & candidate_expanded
        & domain_signals
    )
    domain_bonus = 0.28 if shared_domain else 0.0

    action_signals = {
        "instal",
        "coloc",
        "mont",
        "fix",
        "desinstal",
        "remov",
        "retir",
        "realoc",
        "remanej",
        "limp",
        "higien",
        "lav",
        "consert",
        "repar",
        "recarg",
        "vaz",
        "eletric",
    }
    shared_action = (
        query_expanded
        & candidate_expanded
        & action_signals
    )
    action_bonus = 0.22 if shared_action else 0.0

    return min(
        1.0,
        base_score + capacity_bonus + domain_bonus + action_bonus,
    )


def _semantic_similarity(query: str, candidate: str) -> float:
    left = _expanded_tokens(query) - _SEMANTIC_STOPWORDS
    right = _expanded_tokens(normalize_service_text(candidate)) - _SEMANTIC_STOPWORDS
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    jaccard = intersection / union if union else 0.0
    coverage = intersection / min(len(left), len(right))
    exact_bonus = 0.35 if normalize_service_text(candidate) in query or query in normalize_service_text(candidate) else 0.0
    return min(1.0, (jaccard * 0.45) + (coverage * 0.55) + exact_bonus)


def _expanded_tokens(value: str) -> set[str]:
    normalized = normalize_service_text(value)
    tokens = set(normalized.split())
    expanded = set(tokens)
    for token in tuple(tokens):
        root = _semantic_root(token)
        if root:
            expanded.add(root)
    for token, synonyms in _DOMAIN_EXPANSIONS.items():
        if token in tokens or any(
            _contains_phrase(normalized, normalize_service_text(item))
            for item in synonyms
        ):
            expanded.add(token)
            root = _semantic_root(token)
            if root:
                expanded.add(root)
            for synonym in synonyms:
                synonym_tokens = normalize_service_text(synonym).split()
                expanded.update(synonym_tokens)
                for synonym_token in synonym_tokens:
                    synonym_root = _semantic_root(synonym_token)
                    if synonym_root:
                        expanded.add(synonym_root)
    return expanded


def _semantic_root(token: str) -> str | None:
    if token.startswith("capacidade_"):
        return token
    if len(token) < 5:
        return None
    for suffix in ("acoes", "acao"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    for suffix in ("ivas", "ivos", "iva", "ivo"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    for suffix in ("ar", "er", "ir"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return None


def _contains_phrase(value: str, phrase: str) -> bool:
    if not phrase:
        return False
    return f" {phrase} " in f" {value} "

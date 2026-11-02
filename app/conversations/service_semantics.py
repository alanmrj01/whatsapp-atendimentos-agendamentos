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
    "ar",
    "condicionado",
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
    "split",
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
    for variant in variants:
        for template in _GENERIC_TEMPLATES:
            sentence = template.format(service=variant)
            key = normalize_service_text(sentence)
            if key and key not in seen:
                seen.add(key)
                examples.append(sentence)
            if len(examples) >= 32:
                return tuple(examples)
    return tuple(examples)


def semantic_service_score(
    message: str,
    service_name: str,
    examples: Iterable[str],
) -> float:
    query = normalize_service_text(message)
    if not query:
        return 0.0
    if any(_contains_phrase(query, phrase) for phrase in _PERSONAL_NON_SERVICE_PHRASES):
        return 0.0
    candidates = (service_name, *tuple(examples))
    return max((_semantic_similarity(query, value) for value in candidates), default=0.0)


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

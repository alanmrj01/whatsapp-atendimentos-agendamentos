from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_GENERIC_TEMPLATES = (
    "quero {service}",
    "preciso de {service}",
    "vocês fazem {service}",
    "quanto custa {service}",
    "qual o valor de {service}",
    "quero agendar {service}",
    "tem horário para {service}",
    "preciso marcar {service}",
    "gostaria de fazer {service}",
    "estou precisando de {service}",
    "pode fazer {service}",
    "quero orçamento para {service}",
)

_DOMAIN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "limpeza": ("higienização", "higienizar", "limpar", "lavagem", "lavar"),
    "higienizacao": ("limpeza", "higienizar", "limpar", "lavagem"),
    "instalacao": ("instalar", "colocar", "montagem"),
    "manutencao": ("revisão", "revisar", "conserto", "arrumar"),
    "diagnostico": ("avaliar", "verificar", "descobrir o problema"),
    "gas": ("recarga de gás", "colocar gás", "completar gás"),
    "vazamento": ("teste de vazamento", "procurar vazamento"),
    "split": ("ar condicionado", "ar-condicionado", "aparelho", "ar"),
}


def normalize_service_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


def generate_service_intent_examples(name: str) -> tuple[str, ...]:
    service = " ".join(name.split())
    normalized = normalize_service_text(service)
    variants = {service}
    tokens = set(normalized.split())
    for token, expansions in _DOMAIN_EXPANSIONS.items():
        if token in tokens:
            variants.update(expansions)
    if "ar" in tokens or "condicionado" in tokens or "split" in tokens:
        variants.update(("ar condicionado", "ar-condicionado", "split", "aparelho"))

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
    candidates = (service_name, *tuple(examples))
    return max((_semantic_similarity(query, value) for value in candidates), default=0.0)


def _semantic_similarity(query: str, candidate: str) -> float:
    left = _expanded_tokens(query)
    right = _expanded_tokens(normalize_service_text(candidate))
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    jaccard = intersection / union if union else 0.0
    coverage = intersection / min(len(left), len(right))
    exact_bonus = 0.35 if normalize_service_text(candidate) in query or query in normalize_service_text(candidate) else 0.0
    return min(1.0, (jaccard * 0.45) + (coverage * 0.55) + exact_bonus)


def _expanded_tokens(value: str) -> set[str]:
    tokens = set(normalize_service_text(value).split())
    expanded = set(tokens)
    for token, synonyms in _DOMAIN_EXPANSIONS.items():
        if token in tokens or any(normalize_service_text(item) in value for item in synonyms):
            expanded.add(token)
            for synonym in synonyms:
                expanded.update(normalize_service_text(synonym).split())
    return expanded

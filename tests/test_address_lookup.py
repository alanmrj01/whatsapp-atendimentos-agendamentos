from __future__ import annotations

import pytest

from app.operations.address_lookup import (
    PostalAddress,
    PostalAddressLookupError,
    format_company_address,
    normalize_postal_code,
    validate_address_matches_lookup,
)


def test_normalize_postal_code_accepts_mask_and_rejects_invalid_values() -> None:
    assert normalize_postal_code("12240-000") == "12240000"
    with pytest.raises(PostalAddressLookupError):
        normalize_postal_code("1224")


def test_address_must_match_nonempty_viacep_components() -> None:
    lookup = PostalAddress(
        postal_code="12240000",
        street="Rua Teste",
        neighborhood="Centro",
        city="São José dos Campos",
        state="SP",
    )
    validate_address_matches_lookup(
        lookup=lookup,
        street="Rua Teste",
        neighborhood="Centro",
        city="Sao Jose dos Campos",
        state="SP",
    )
    with pytest.raises(PostalAddressLookupError, match="Cidade"):
        validate_address_matches_lookup(
            lookup=lookup,
            street="Rua Teste",
            neighborhood="Centro",
            city="Campinas",
            state="SP",
        )


def test_broad_cep_can_accept_required_manual_street_and_neighborhood() -> None:
    lookup = PostalAddress(
        postal_code="12345678",
        street="",
        neighborhood="",
        city="Cidade",
        state="SP",
    )
    validate_address_matches_lookup(
        lookup=lookup,
        street="Rua preenchida manualmente",
        neighborhood="Bairro preenchido manualmente",
        city="Cidade",
        state="SP",
    )


def test_format_company_address_is_canonical() -> None:
    assert format_company_address(
        street="Rua Teste",
        number="160",
        neighborhood="Centro",
        city="São José dos Campos",
        state="sp",
        postal_code="12240000",
    ) == "Rua Teste, 160 - Centro, São José dos Campos - SP, CEP 12240-000"

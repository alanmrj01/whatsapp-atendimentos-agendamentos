from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

import httpx


_VIACEP_URL = "https://viacep.com.br/ws/{postal_code}/json/"


class PostalAddressLookupError(RuntimeError):
    """Safe, user-facing failure while validating a Brazilian postal address."""


@dataclass(frozen=True, slots=True)
class PostalAddress:
    postal_code: str
    street: str
    neighborhood: str
    city: str
    state: str


def normalize_postal_code(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"[0-9]{8}", digits):
        raise PostalAddressLookupError("CEP inválido")
    return digits


def _normalized_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", " ".join(value.split()).casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


async def lookup_postal_address(postal_code: str) -> PostalAddress:
    normalized = normalize_postal_code(postal_code)
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(
                _VIACEP_URL.format(postal_code=normalized),
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise PostalAddressLookupError(
            "Não foi possível validar o CEP agora. Tente novamente."
        ) from exc

    if response.status_code != 200:
        raise PostalAddressLookupError("CEP inválido")

    try:
        payload = response.json()
    except ValueError as exc:
        raise PostalAddressLookupError(
            "Não foi possível validar o CEP agora. Tente novamente."
        ) from exc

    if payload.get("erro") is True:
        raise PostalAddressLookupError("CEP não encontrado")

    city = " ".join(str(payload.get("localidade") or "").split())
    state = " ".join(str(payload.get("uf") or "").split()).upper()
    if not city or not re.fullmatch(r"[A-Z]{2}", state):
        raise PostalAddressLookupError("CEP sem cidade/UF válidos")

    return PostalAddress(
        postal_code=normalized,
        street=" ".join(str(payload.get("logradouro") or "").split()),
        neighborhood=" ".join(str(payload.get("bairro") or "").split()),
        city=city,
        state=state,
    )


def validate_address_matches_lookup(
    *,
    lookup: PostalAddress,
    street: str,
    neighborhood: str,
    city: str,
    state: str,
) -> None:
    comparisons = (
        (lookup.street, street, "Rua não corresponde ao CEP informado"),
        (lookup.neighborhood, neighborhood, "Bairro não corresponde ao CEP informado"),
        (lookup.city, city, "Cidade não corresponde ao CEP informado"),
        (lookup.state, state, "UF não corresponde ao CEP informado"),
    )
    for expected, actual, message in comparisons:
        # Some valid CEPs are broad and ViaCEP can omit street/neighborhood.
        # In that case the required manually supplied value is accepted.
        if expected and _normalized_text(expected) != _normalized_text(actual):
            raise PostalAddressLookupError(message)


def format_company_address(
    *,
    street: str,
    number: str,
    neighborhood: str,
    city: str,
    state: str,
    postal_code: str,
) -> str:
    return (
        f"{' '.join(street.split())}, {' '.join(number.split())} - "
        f"{' '.join(neighborhood.split())}, {' '.join(city.split())} - "
        f"{state.strip().upper()}, CEP {postal_code[:5]}-{postal_code[5:]}"
    )

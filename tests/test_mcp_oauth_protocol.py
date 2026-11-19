from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    CHATGPT_REDIRECT_URI,
    MCP_READ_SCOPE,
    OAuthProtocolError,
    authorization_redirect,
    authorization_server_metadata,
    pkce_s256_matches,
    validate_authorization_request,
    validate_chatgpt_cimd,
    validate_token_request,
)


ISSUER = "https://backend.example.com"
RESOURCE = "https://backend.example.com/mcp-v2"

VERIFIER = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-._~"
)

CHALLENGE = (
    base64.urlsafe_b64encode(
        hashlib.sha256(
            VERIFIER.encode("ascii")
        ).digest()
    )
    .rstrip(b"=")
    .decode("ascii")
)


def _cimd() -> dict[str, object]:
    return {
        "client_id": CHATGPT_CLIENT_ID,
        "redirect_uris": [CHATGPT_REDIRECT_URI],
        "response_types": ["code"],
        "grant_types": [
            "authorization_code",
            "refresh_token",
        ],
        "token_endpoint_auth_methods_supported": [
            "none",
            "private_key_jwt",
        ],
    }


def _authorize_params() -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": CHATGPT_CLIENT_ID,
        "redirect_uri": CHATGPT_REDIRECT_URI,
        "state": "state-123",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": f"{MCP_READ_SCOPE} offline_access",
        "resource": RESOURCE,
    }


def test_metadata_is_chatgpt_compatible() -> None:
    metadata = authorization_server_metadata(
        issuer=ISSUER
    )

    assert metadata == {
        "issuer": ISSUER,
        "authorization_endpoint": (
            f"{ISSUER}/oauth/authorize"
        ),
        "token_endpoint": f"{ISSUER}/oauth/token",
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        "scopes_supported": [
            "alovia.read",
            "offline_access",
        ],
    }


def test_current_chatgpt_cimd_contract_is_accepted() -> None:
    validate_chatgpt_cimd(_cimd())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("client_id", "https://evil.example/client.json"),
        ("redirect_uris", ["https://evil.example/callback"]),
        ("response_types", ["token"]),
        ("grant_types", ["authorization_code"]),
        (
            "token_endpoint_auth_methods_supported",
            ["private_key_jwt"],
        ),
    ),
)
def test_invalid_cimd_is_rejected(
    field: str,
    value: object,
) -> None:
    document = _cimd()
    document[field] = value

    with pytest.raises(OAuthProtocolError):
        validate_chatgpt_cimd(document)


def test_authorization_request_requires_exact_contract() -> None:
    request = validate_authorization_request(
        _authorize_params(),
        expected_resource=RESOURCE,
    )

    assert request.client_id == CHATGPT_CLIENT_ID
    assert request.redirect_uri == CHATGPT_REDIRECT_URI
    assert request.resource == RESOURCE
    assert request.scopes == (
        "alovia.read",
        "offline_access",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("response_type", "token"),
        ("client_id", "https://evil.example/client.json"),
        ("redirect_uri", "https://evil.example/callback"),
        ("state", ""),
        ("code_challenge_method", "plain"),
        ("code_challenge", "short"),
        ("scope", "offline_access"),
        ("scope", "alovia.read evil.scope"),
        ("resource", "https://evil.example/mcp-v2"),
    ),
)
def test_bad_authorization_request_is_rejected(
    field: str,
    value: str,
) -> None:
    params = _authorize_params()
    params[field] = value

    with pytest.raises(OAuthProtocolError):
        validate_authorization_request(
            params,
            expected_resource=RESOURCE,
        )


def test_authorization_code_token_request() -> None:
    request = validate_token_request(
        {
            "grant_type": "authorization_code",
            "client_id": CHATGPT_CLIENT_ID,
            "resource": RESOURCE,
            "code": "opaque-code",
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "code_verifier": VERIFIER,
        },
        expected_resource=RESOURCE,
    )

    assert request.grant_type == "authorization_code"
    assert request.code == "opaque-code"
    assert request.code_verifier == VERIFIER


def test_refresh_token_request() -> None:
    request = validate_token_request(
        {
            "grant_type": "refresh_token",
            "client_id": CHATGPT_CLIENT_ID,
            "resource": RESOURCE,
            "refresh_token": "opaque-refresh-token",
        },
        expected_resource=RESOURCE,
    )

    assert request.grant_type == "refresh_token"
    assert request.refresh_token == (
        "opaque-refresh-token"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("client_id", "https://evil.example/client.json"),
        ("resource", "https://evil.example/mcp-v2"),
        ("redirect_uri", "https://evil.example/callback"),
        ("code_verifier", "short"),
    ),
)
def test_bad_code_exchange_is_rejected(
    field: str,
    value: str,
) -> None:
    form = {
        "grant_type": "authorization_code",
        "client_id": CHATGPT_CLIENT_ID,
        "resource": RESOURCE,
        "code": "opaque-code",
        "redirect_uri": CHATGPT_REDIRECT_URI,
        "code_verifier": VERIFIER,
    }
    form[field] = value

    with pytest.raises(OAuthProtocolError):
        validate_token_request(
            form,
            expected_resource=RESOURCE,
        )


def test_pkce_s256_is_constant_time_checked() -> None:
    assert pkce_s256_matches(
        VERIFIER,
        CHALLENGE,
    )

    assert not pkce_s256_matches(
        VERIFIER + "x",
        CHALLENGE,
    )


def test_success_redirect_always_contains_issuer() -> None:
    redirect = authorization_redirect(
        issuer=ISSUER,
        redirect_uri=CHATGPT_REDIRECT_URI,
        state="state-123",
        code="authorization-code",
    )

    query = parse_qs(urlsplit(redirect).query)

    assert query == {
        "state": ["state-123"],
        "iss": [ISSUER],
        "code": ["authorization-code"],
    }


def test_error_redirect_always_contains_issuer() -> None:
    redirect = authorization_redirect(
        issuer=ISSUER,
        redirect_uri=CHATGPT_REDIRECT_URI,
        state="state-123",
        error="access_denied",
    )

    query = parse_qs(urlsplit(redirect).query)

    assert query == {
        "state": ["state-123"],
        "iss": [ISSUER],
        "error": ["access_denied"],
    }


@pytest.mark.parametrize(
    "issuer",
    (
        "http://backend.example.com",
        "https://backend.example.com/",
        "https://backend.example.com/path",
        "not-a-url",
    ),
)
def test_issuer_must_be_canonical_https(
    issuer: str,
) -> None:
    with pytest.raises(OAuthProtocolError):
        authorization_server_metadata(
            issuer=issuer
        )

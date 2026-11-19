from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from collections.abc import Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit


CHATGPT_CLIENT_ID = "https://chatgpt.com/oauth/client.json"
CHATGPT_REDIRECT_URI = (
    "https://chatgpt.com/connector_platform_oauth_redirect"
)

MCP_READ_SCOPE = "alovia.read"
OFFLINE_SCOPE = "offline_access"

SUPPORTED_SCOPES = (
    MCP_READ_SCOPE,
    OFFLINE_SCOPE,
)

_CODE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_CODE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class OAuthProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    scopes: tuple[str, ...]
    resource: str


@dataclass(frozen=True, slots=True)
class TokenRequest:
    grant_type: str
    client_id: str
    resource: str
    code: str | None = None
    redirect_uri: str | None = None
    code_verifier: str | None = None
    refresh_token: str | None = None
    scopes: tuple[str, ...] = ()


def validate_issuer(value: str) -> str:
    raw = value.strip()

    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise OAuthProtocolError("invalid issuer") from None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or raw.endswith("/")
    ):
        raise OAuthProtocolError("invalid issuer")

    return raw


def validate_resource(value: str) -> str:
    raw = value.strip()

    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise OAuthProtocolError("invalid resource") from None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/mcp-v2"
        or raw.endswith("/")
    ):
        raise OAuthProtocolError("invalid resource")

    return raw


def authorization_server_metadata(
    *,
    issuer: str,
) -> dict[str, object]:
    canonical = validate_issuer(issuer)

    return {
        "issuer": canonical,
        "authorization_endpoint": f"{canonical}/oauth/authorize",
        "token_endpoint": f"{canonical}/oauth/token",
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        "scopes_supported": list(SUPPORTED_SCOPES),
    }


def validate_chatgpt_cimd(
    document: Mapping[str, object],
) -> None:
    if document.get("client_id") != CHATGPT_CLIENT_ID:
        raise OAuthProtocolError("invalid CIMD client")

    redirect_uris = document.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or CHATGPT_REDIRECT_URI not in redirect_uris
    ):
        raise OAuthProtocolError("invalid CIMD redirect URI")

    response_types = document.get("response_types")
    if (
        not isinstance(response_types, list)
        or "code" not in response_types
    ):
        raise OAuthProtocolError("CIMD does not support code")

    grant_types = document.get("grant_types")
    if (
        not isinstance(grant_types, list)
        or "authorization_code" not in grant_types
        or "refresh_token" not in grant_types
    ):
        raise OAuthProtocolError("invalid CIMD grant types")

    methods = document.get(
        "token_endpoint_auth_methods_supported"
    )
    if (
        not isinstance(methods, list)
        or "none" not in methods
    ):
        raise OAuthProtocolError(
            "CIMD does not support public client"
        )


def _parse_scopes(raw: str) -> tuple[str, ...]:
    scopes = tuple(
        dict.fromkeys(
            item
            for item in raw.strip().split()
            if item
        )
    )

    if MCP_READ_SCOPE not in scopes:
        raise OAuthProtocolError("required scope missing")

    if any(scope not in SUPPORTED_SCOPES for scope in scopes):
        raise OAuthProtocolError("unsupported scope")

    return scopes


def validate_authorization_request(
    params: Mapping[str, str],
    *,
    expected_resource: str,
) -> AuthorizationRequest:
    resource = validate_resource(expected_resource)

    if params.get("response_type") != "code":
        raise OAuthProtocolError("unsupported response type")

    if params.get("client_id") != CHATGPT_CLIENT_ID:
        raise OAuthProtocolError("invalid client")

    if params.get("redirect_uri") != CHATGPT_REDIRECT_URI:
        raise OAuthProtocolError("invalid redirect URI")

    state = params.get("state", "")
    if not state or len(state) > 2048:
        raise OAuthProtocolError("invalid state")

    if params.get("code_challenge_method") != "S256":
        raise OAuthProtocolError("PKCE S256 required")

    challenge = params.get("code_challenge", "")
    if not _CODE_CHALLENGE.fullmatch(challenge):
        raise OAuthProtocolError("invalid code challenge")

    requested_resource = params.get("resource", "")
    if requested_resource != resource:
        raise OAuthProtocolError("invalid resource")

    scopes = _parse_scopes(params.get("scope", ""))

    return AuthorizationRequest(
        client_id=CHATGPT_CLIENT_ID,
        redirect_uri=CHATGPT_REDIRECT_URI,
        state=state,
        code_challenge=challenge,
        scopes=scopes,
        resource=resource,
    )


def validate_token_request(
    form: Mapping[str, str],
    *,
    expected_resource: str,
) -> TokenRequest:
    resource = validate_resource(expected_resource)

    client_id = form.get("client_id", "")
    if client_id != CHATGPT_CLIENT_ID:
        raise OAuthProtocolError("invalid client")

    requested_resource = form.get("resource", "")
    if requested_resource != resource:
        raise OAuthProtocolError("invalid resource")

    grant_type = form.get("grant_type", "")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        verifier = form.get("code_verifier", "")

        if not code or len(code) > 512:
            raise OAuthProtocolError("invalid code")

        if redirect_uri != CHATGPT_REDIRECT_URI:
            raise OAuthProtocolError("invalid redirect URI")

        if not _CODE_VERIFIER.fullmatch(verifier):
            raise OAuthProtocolError("invalid code verifier")

        return TokenRequest(
            grant_type=grant_type,
            client_id=client_id,
            resource=resource,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
        )

    if grant_type == "refresh_token":
        token = form.get("refresh_token", "")

        if not token or len(token) > 512:
            raise OAuthProtocolError("invalid refresh token")

        raw_scope = form.get("scope", "").strip()
        scopes = (
            _parse_scopes(raw_scope)
            if raw_scope
            else ()
        )

        return TokenRequest(
            grant_type=grant_type,
            client_id=client_id,
            resource=resource,
            refresh_token=token,
            scopes=scopes,
        )

    raise OAuthProtocolError("unsupported grant type")


def pkce_s256_matches(
    verifier: str,
    expected_challenge: str,
) -> bool:
    if (
        not _CODE_VERIFIER.fullmatch(verifier)
        or not _CODE_CHALLENGE.fullmatch(
            expected_challenge
        )
    ):
        return False

    digest = hashlib.sha256(
        verifier.encode("ascii")
    ).digest()

    calculated = (
        base64.urlsafe_b64encode(digest)
        .rstrip(b"=")
        .decode("ascii")
    )

    return hmac.compare_digest(
        calculated,
        expected_challenge,
    )


def authorization_redirect(
    *,
    issuer: str,
    redirect_uri: str,
    state: str,
    code: str | None = None,
    error: str | None = None,
) -> str:
    canonical_issuer = validate_issuer(issuer)

    if redirect_uri != CHATGPT_REDIRECT_URI:
        raise OAuthProtocolError("invalid redirect URI")

    if not state or len(state) > 2048:
        raise OAuthProtocolError("invalid state")

    if (code is None) == (error is None):
        raise OAuthProtocolError(
            "exactly one authorization result is required"
        )

    query: dict[str, str] = {
        "state": state,
        "iss": canonical_issuer,
    }

    if code is not None:
        if not code or len(code) > 512:
            raise OAuthProtocolError("invalid code")
        query["code"] = code
    else:
        assert error is not None
        if not error or len(error) > 128:
            raise OAuthProtocolError("invalid error")
        query["error"] = error

    parsed = urlsplit(redirect_uri)

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            "",
        )
    )

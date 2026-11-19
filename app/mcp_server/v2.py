from __future__ import annotations

from collections.abc import Iterable

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from app.mcp_server.runtime import (
    MCPConfigurationError,
    MCPRuntime,
    normalize_allowed_hosts,
)


_http_url = TypeAdapter(AnyHttpUrl)


def _secure_http_url(value: str, *, field: str) -> AnyHttpUrl:
    try:
        parsed = _http_url.validate_python(value.strip())
    except ValidationError:
        raise MCPConfigurationError(
            f"Invalid MCP V2 {field}"
        ) from None

    if parsed.scheme != "https":
        raise MCPConfigurationError(
            f"MCP V2 {field} must use HTTPS"
        )

    if not parsed.host:
        raise MCPConfigurationError(
            f"Invalid MCP V2 {field}"
        )

    return parsed


def build_mcp_v2_runtime(
    *,
    allowed_hosts: Iterable[str],
    issuer_url: str,
    resource_server_url: str,
    token_verifier: TokenVerifier,
    required_scope: str = "alovia.read",
) -> MCPRuntime:
    hosts = normalize_allowed_hosts(allowed_hosts)

    issuer = _secure_http_url(
        issuer_url,
        field="issuer URL",
    )
    resource = _secure_http_url(
        resource_server_url,
        field="resource URL",
    )

    scope = required_scope.strip()
    if not scope or any(character.isspace() for character in scope):
        raise MCPConfigurationError(
            "Invalid MCP V2 required scope"
        )

    server = MCPServer(
        "alovia",
        title="Alovia MCP V2",
        description="Servidor MCP autenticado da Alovia.",
        instructions=(
            "V2 OAuth foundation. "
            "Nenhuma ferramenta privada está publicada nesta etapa."
        ),
        version="2.0.0",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            required_scopes=[scope],
            validate_token_resource=True,
        ),
    )

    app = server.streamable_http_app(
        streamable_http_path="/mcp-v2",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1_048_576,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(hosts),
            allowed_origins=[],
        ),
    )

    return MCPRuntime(
        server=server,
        app=app,
    )

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from app.core.database import get_session_factory
from app.models.domain import Business
from app.mcp_server.runtime import (
    MCPConfigurationError,
    MCPRuntime,
    normalize_allowed_hosts,
)


_http_url = TypeAdapter(AnyHttpUrl)


class MCPProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


BusinessProfileLoader = Callable[
    [UUID],
    Awaitable[MCPProfile | None],
]


async def _load_business_profile(
    business_id: UUID,
) -> MCPProfile | None:
    session_factory = get_session_factory()

    async with session_factory() as session:
        business = await session.get(
            Business,
            business_id,
        )

    if business is None or not business.active:
        return None

    name = business.name.strip()

    if not name:
        return None

    return MCPProfile(
        id=str(business.id),
        name=name,
    )


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
    profile_loader: BusinessProfileLoader | None = None,
    required_scope: str = "alovia.read",
) -> MCPRuntime:
    hosts = normalize_allowed_hosts(allowed_hosts)

    issuer = issuer_url.strip()
    _secure_http_url(
        issuer,
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

    load_profile = (
        profile_loader
        if profile_loader is not None
        else _load_business_profile
    )

    server = MCPServer(
        "alovia",
        title="Alovia MCP V2",
        description="Servidor MCP autenticado da Alovia.",
        instructions=(
            "Servidor autenticado da Alovia. "
            "Ferramentas privadas usam exclusivamente a empresa "
            "associada às credenciais OAuth da conexão."
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

    @server.tool(
        name="get_profile",
        title="Perfil da empresa Alovia",
        description=(
            "Retorna o perfil da empresa Alovia representada "
            "pelas credenciais autenticadas desta conexão."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        meta={
            "openai/profile": True,
            "securitySchemes": [
                {
                    "type": "oauth2",
                    "scopes": [scope],
                }
            ],
        },
        structured_output=True,
    )
    async def get_profile() -> MCPProfile:
        access_token = get_access_token()

        claims = (
            access_token.claims
            if access_token is not None
            else None
        )

        raw_business_id = (
            claims.get("business_id")
            if isinstance(claims, dict)
            else None
        )

        if not isinstance(raw_business_id, str):
            raise ValueError(
                "Authenticated business is unavailable"
            )

        try:
            business_id = UUID(raw_business_id)
        except ValueError:
            raise ValueError(
                "Authenticated business is unavailable"
            ) from None

        profile = await load_profile(business_id)

        if profile is None:
            raise ValueError(
                "Authenticated business is unavailable"
            )

        return profile

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

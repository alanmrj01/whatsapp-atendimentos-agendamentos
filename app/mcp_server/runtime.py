from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.applications import Starlette


class MCPConfigurationError(RuntimeError):
    """Configuração MCP ausente ou insegura."""


class MCPServerStatus(BaseModel):
    service: str
    mcp_version: str
    mode: str


@dataclass(frozen=True, slots=True)
class MCPRuntime:
    server: MCPServer
    app: Starlette


_HOST_PATTERN = re.compile(
    r"(?:localhost|127\.0\.0\.1|"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r"(?::(?:\*|\d{1,5}))?"
)


def normalize_allowed_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []

    for raw in hosts:
        host = raw.strip()
        if not host:
            continue
        if not _HOST_PATTERN.fullmatch(host):
            raise MCPConfigurationError("Invalid MCP allowed host")
        if host not in normalized:
            normalized.append(host)

    if not normalized:
        raise MCPConfigurationError("At least one MCP allowed host is required")

    return tuple(normalized)


def build_mcp_runtime(*, allowed_hosts: Iterable[str]) -> MCPRuntime:
    hosts = normalize_allowed_hosts(allowed_hosts)

    server = MCPServer(
        "alovia",
        title="Alovia MCP",
        description="Servidor MCP da Alovia.",
        instructions=(
            "Use apenas as capacidades explicitamente publicadas pelo servidor. "
            "A V1 não expõe dados privados sem autenticação."
        ),
        version="1.0.0",
    )

    @server.tool(
        name="server_status",
        title="Alovia MCP status",
        description=(
            "Retorna somente metadados públicos e não sensíveis sobre o servidor "
            "MCP. Não acessa banco, empresas, clientes ou integrações."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            open_world_hint=False,
        ),
    )
    def server_status() -> MCPServerStatus:
        return MCPServerStatus(
            service="alovia",
            mcp_version="1.0.0",
            mode="diagnostic_only",
        )

    app = server.streamable_http_app(
        streamable_http_path="/",
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

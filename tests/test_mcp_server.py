from __future__ import annotations

import pytest
from mcp import Client

from app.mcp_server.runtime import (
    MCPConfigurationError,
    build_mcp_runtime,
    normalize_allowed_hosts,
)


def test_mcp_allowed_hosts_are_explicit_and_deduplicated() -> None:
    assert normalize_allowed_hosts(
        (
            "mcp.example.com",
            "mcp.example.com:*",
            "mcp.example.com",
        )
    ) == (
        "mcp.example.com",
        "mcp.example.com:*",
    )


@pytest.mark.parametrize(
    "host",
    (
        "",
        "https://mcp.example.com",
        "mcp.example.com/path",
        "*.example.com",
        "mcp example.com",
    ),
)
def test_mcp_rejects_unsafe_allowed_hosts(host: str) -> None:
    with pytest.raises(MCPConfigurationError):
        normalize_allowed_hosts((host,))


@pytest.mark.asyncio
async def test_mcp_v1_bootstrap_negotiates_with_no_tools() -> None:
    runtime = build_mcp_runtime(
        allowed_hosts=("localhost", "localhost:*"),
    )

    async with Client(runtime.server, raise_exceptions=True) as client:
        assert client.server_info is not None
        assert client.server_info.name == "alovia"
        assert client.server_info.version == "1.0.0"

        tools = await client.list_tools()

    assert tools.tools == []

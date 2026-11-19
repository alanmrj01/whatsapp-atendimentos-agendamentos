import pytest
from pydantic import SecretStr

import app.main as main_module
from app.core.config import Settings
from app.mcp_server.runtime import MCPConfigurationError


def test_oauth_cannot_be_enabled_without_mcp_v2(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None
    ).model_copy(
        update={
            "mcp_oauth_enabled": True,
            "mcp_v2_enabled": False,
            "mcp_oauth_signing_key": SecretStr(
                "x" * 32
            ),
        }
    )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: settings,
    )

    with pytest.raises(
        MCPConfigurationError,
        match=(
            "MCP OAuth requires MCP V2 "
            "to be enabled"
        ),
    ):
        main_module.create_app()

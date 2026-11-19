from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import Base


TABLE = "mcp_oauth_authorization_flows"


def test_pending_flow_is_registered() -> None:
    assert TABLE in Base.metadata.tables


def test_pending_flow_is_server_bound() -> None:
    table = Base.metadata.tables[TABLE]

    expected = {
        "flow_token_hash",
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "scope",
        "resource",
        "user_id",
        "business_id",
        "expires_at",
        "consumed_at",
    }

    assert expected.issubset(table.c.keys())


def test_pending_flow_never_persists_raw_flow_token() -> None:
    table = Base.metadata.tables[TABLE]

    assert "flow_token_hash" in table.c
    assert "flow_token" not in table.c

    forbidden = {
        "password",
        "access_token",
        "refresh_token",
        "authorization_code",
        "client_secret",
    }

    assert forbidden.isdisjoint(table.c.keys())


def test_pending_flow_user_and_business_are_server_links() -> None:
    table = Base.metadata.tables[TABLE]

    targets = {
        foreign_key.target_fullname
        for foreign_key in table.foreign_keys
    }

    assert "users.id" in targets
    assert "businesses.id" in targets


def test_pending_flow_can_exist_before_login() -> None:
    table = Base.metadata.tables[TABLE]

    assert table.c.user_id.nullable is True
    assert table.c.business_id.nullable is True


def test_flow_migration_extends_oauth_foundation() -> None:
    path = Path(
        "alembic/versions/"
        "20261118_0046_mcp_oauth_authorization_flows.py"
    )

    spec = importlib.util.spec_from_file_location(
        "mcp_oauth_migration_0046",
        path,
    )

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "20261118_0046"
    assert module.down_revision == "20261118_0045"

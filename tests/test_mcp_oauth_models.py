from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models import Base


def test_mcp_oauth_tables_are_additive() -> None:
    names = set(Base.metadata.tables)

    assert "mcp_oauth_authorization_codes" in names
    assert "mcp_oauth_sessions" in names


def test_mcp_oauth_never_persists_raw_credentials() -> None:
    code = Base.metadata.tables[
        "mcp_oauth_authorization_codes"
    ]
    session = Base.metadata.tables[
        "mcp_oauth_sessions"
    ]

    assert "code_hash" in code.c
    assert "refresh_token_hash" in session.c

    forbidden = {
        "authorization_code",
        "refresh_token",
        "access_token",
        "password",
        "client_secret",
    }

    assert forbidden.isdisjoint(code.c.keys())
    assert forbidden.isdisjoint(session.c.keys())


def test_mcp_oauth_rows_are_user_and_business_scoped() -> None:
    for table_name in (
        "mcp_oauth_authorization_codes",
        "mcp_oauth_sessions",
    ):
        table = Base.metadata.tables[table_name]

        assert "user_id" in table.c
        assert "business_id" in table.c
        assert "client_id" in table.c
        assert "scope" in table.c
        assert "resource" in table.c

        targets = {
            foreign_key.target_fullname
            for foreign_key in table.foreign_keys
        }

        assert "users.id" in targets
        assert "businesses.id" in targets


def test_mcp_oauth_migration_extends_current_head() -> None:
    path = Path(
        "alembic/versions/"
        "20261118_0045_mcp_oauth_foundation.py"
    )

    spec = importlib.util.spec_from_file_location(
        "mcp_oauth_migration_0045",
        path,
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "20261118_0045"
    assert module.down_revision == "20261111_0044"

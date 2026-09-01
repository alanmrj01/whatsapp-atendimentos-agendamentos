from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch
from sqlalchemy.pool import NullPool

from app.core import database
from app.core.config import DatabaseConfigurationError, Settings


class FakeSessionContext:
    def __init__(self) -> None:
        self.session = object()
        self.closed = False

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_: Any) -> None:
        self.closed = True


def test_engine_is_created_lazily_with_pre_ping(monkeypatch: MonkeyPatch) -> None:
    created_with: dict[str, Any] = {}
    fake_engine = object()

    class FakeSettings:
        def require_database_url(self) -> str:
            return "postgresql+asyncpg://user:placeholder@example.invalid/db"

    def fake_create_async_engine(url: str, **options: Any) -> object:
        created_with["url"] = url
        created_with.update(options)
        return fake_engine

    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)

    assert database.get_engine() is fake_engine
    assert database.get_engine() is fake_engine
    assert created_with["pool_pre_ping"] is True


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://user@example.invalid:5432/postgres",
        "postgresql+asyncpg://user@db.example.invalid:5432/postgres",
    ],
    ids=["session-pooler", "direct"],
)
def test_session_and_direct_connections_keep_standard_pool(
    database_url: str,
) -> None:
    options = database.build_engine_options(database_url)

    assert options == {"pool_pre_ping": True}


def test_transaction_pooler_uses_null_pool_and_disables_statement_caches() -> None:
    options = database.build_engine_options(
        "postgresql+asyncpg://user@example.invalid:6543/postgres"
    )

    assert options["pool_pre_ping"] is True
    assert options["poolclass"] is NullPool
    assert options["connect_args"] == {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }


@pytest.mark.asyncio
async def test_get_db_closes_session(monkeypatch: MonkeyPatch) -> None:
    session_context = FakeSessionContext()
    monkeypatch.setattr(
        database,
        "get_session_factory",
        lambda: lambda: session_context,
    )

    dependency = database.get_db()
    session = await anext(dependency)

    assert session is session_context.session
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
    assert session_context.closed is True


@pytest.mark.asyncio
async def test_database_check_executes_select_one(monkeypatch: MonkeyPatch) -> None:
    executed_statements: list[str] = []

    class FakeConnection:
        async def execute(self, statement: Any) -> None:
            executed_statements.append(str(statement))

    class FakeConnectionContext:
        async def __aenter__(self) -> FakeConnection:
            return FakeConnection()

        async def __aexit__(self, *_: Any) -> None:
            return None

    class FakeEngine:
        def connect(self) -> FakeConnectionContext:
            return FakeConnectionContext()

    monkeypatch.setattr(database, "get_engine", lambda: FakeEngine())

    assert await database.check_database_connection() is True
    assert executed_statements == ["SELECT 1"]


@pytest.mark.asyncio
async def test_dispose_engine_closes_pool(monkeypatch: MonkeyPatch) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    fake_engine = FakeEngine()
    monkeypatch.setattr(database, "engine", fake_engine)
    monkeypatch.setattr(database, "AsyncSessionFactory", object())

    await database.dispose_engine()

    assert fake_engine.disposed is True
    assert database.engine is None
    assert database.AsyncSessionFactory is None


def test_database_url_is_required_only_when_database_is_used(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.database_url is None
    with pytest.raises(
        DatabaseConfigurationError,
        match="DATABASE_URL is required to use the database",
    ):
        settings.require_database_url()


def test_postgresql_url_is_normalized_for_asyncpg() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user:placeholder@example.invalid:5432/postgres",
    )

    assert settings.require_database_url().startswith("postgresql+asyncpg://")


def test_alembic_database_url_falls_back_to_runtime_url() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://user@example.invalid:5432/postgres",
        ALEMBIC_DATABASE_URL="",
    )

    assert settings.require_alembic_database_url() == settings.require_database_url()


def test_alembic_database_url_is_preferred_and_normalized() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://runtime@example.invalid:6543/postgres",
        ALEMBIC_DATABASE_URL="postgresql://migration@example.invalid:5432/postgres",
    )

    assert settings.require_alembic_database_url() == (
        "postgresql+asyncpg://migration@example.invalid:5432/postgres"
    )

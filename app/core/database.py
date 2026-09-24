from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text

from app.core.config import DatabaseConfigurationError, get_settings

engine: AsyncEngine | None = None
AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None
logger = logging.getLogger(__name__)


def is_transaction_pooler_url(database_url: str) -> bool:
    try:
        return make_url(database_url).port == 6543
    except (ArgumentError, ValueError):
        raise DatabaseConfigurationError("DATABASE_URL is invalid") from None


def build_engine_options(database_url: str) -> dict[str, Any]:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if is_transaction_pooler_url(database_url):
        options.update(
            poolclass=NullPool,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
            },
        )
    return options


def get_engine() -> AsyncEngine:
    global engine

    if engine is None:
        database_url = get_settings().require_database_url()
        engine = create_async_engine(
            database_url,
            **build_engine_options(database_url),
        )
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global AsyncSessionFactory

    if AsyncSessionFactory is None:
        AsyncSessionFactory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return AsyncSessionFactory


async def get_db() -> AsyncIterator[AsyncSession]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


get_db_session = get_db


async def check_database_connection() -> bool:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        # Safe observability only: never log exception text, parameters, DSNs,
        # hostnames, usernames, or credentials. SQLSTATE and exception class are
        # sufficient to distinguish auth/network/protocol failures.
        sqlstate = getattr(exc, "sqlstate", None)
        original = getattr(exc, "orig", None)
        if sqlstate is None and original is not None:
            sqlstate = getattr(original, "sqlstate", None)
        logger.warning(
            "database_connectivity_failed",
            extra={
                "error_type": type(exc).__name__,
                "sqlstate": sqlstate if isinstance(sqlstate, str) else None,
            },
        )
        return False
    return True


async def dispose_engine() -> None:
    global engine, AsyncSessionFactory

    if engine is not None:
        await engine.dispose()
    engine = None
    AsyncSessionFactory = None

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

from app.core.config import get_settings

engine: AsyncEngine | None = None
AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global engine

    if engine is None:
        engine = create_async_engine(
            get_settings().require_database_url(),
            pool_pre_ping=True,
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
    except Exception:
        return False
    return True


async def dispose_engine() -> None:
    global engine, AsyncSessionFactory

    if engine is not None:
        await engine.dispose()
    engine = None
    AsyncSessionFactory = None

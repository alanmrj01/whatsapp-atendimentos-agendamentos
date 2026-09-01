import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.update(
    {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
        "ALEMBIC_DATABASE_URL": "",
        "META_ACCESS_TOKEN": "test-access-token",
        "META_PHONE_NUMBER_ID": "test-phone-number-id",
        "META_GRAPH_VERSION": "v23.0",
        "META_WABA_ID": "test-waba-id",
        "META_APP_SECRET": "test-app-secret",
        "META_VERIFY_TOKEN": "test-verify-token",
        "ENVIRONMENT": "test",
    }
)

from app.main import app  # noqa: E402


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client

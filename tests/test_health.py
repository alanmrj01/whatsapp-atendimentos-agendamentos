from httpx import AsyncClient
from pytest import mark

from app.core.database import check_database_connection
from app.main import app


@mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@mark.asyncio
async def test_readiness_returns_ok_when_database_is_connected(
    client: AsyncClient,
) -> None:
    async def connected_database() -> bool:
        return True

    app.dependency_overrides[check_database_connection] = connected_database
    try:
        response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(check_database_connection, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "connected"}


@mark.asyncio
async def test_readiness_returns_503_when_database_is_unavailable(
    client: AsyncClient,
) -> None:
    async def unavailable_database() -> bool:
        return False

    app.dependency_overrides[check_database_connection] = unavailable_database
    try:
        response = await client.get("/ready")
    finally:
        app.dependency_overrides.pop(check_database_connection, None)

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
    }


@mark.asyncio
async def test_database_health_returns_ok_when_database_is_connected(
    client: AsyncClient,
) -> None:
    async def connected_database() -> bool:
        return True

    app.dependency_overrides[check_database_connection] = connected_database
    try:
        response = await client.get("/health/db")
    finally:
        app.dependency_overrides.pop(check_database_connection, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


@mark.asyncio
async def test_database_health_returns_503_when_database_is_unavailable(
    client: AsyncClient,
) -> None:
    async def unavailable_database() -> bool:
        return False

    app.dependency_overrides[check_database_connection] = unavailable_database
    try:
        response = await client.get("/health/db")
    finally:
        app.dependency_overrides.pop(check_database_connection, None)

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}

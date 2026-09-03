import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import require_auth_config
from app.auth.security import access_token, decode_access, hash_password, token_hash, verify_password
from app.auth.schemas import LoginRequest
from app.core.config import Settings, get_settings
from app.main import create_app
from tests.test_migration import PROJECT_ROOT, render_migration_sql


def test_argon2id_random_salt_and_no_plaintext():
    password = secrets.token_urlsafe(24)
    first, second = hash_password(password), hash_password(password)
    assert first.startswith("$argon2id$") and first != second
    assert password not in first and verify_password(password, first)
    assert not verify_password("incorrect", first)
    assert not verify_password(password, None)
    assert len(token_hash(password)) == 64 and token_hash(password) != password
    assert password not in repr(LoginRequest(email="user@example.test", password=password))


@pytest.mark.parametrize("kind", ["expired", "forged", "none", "missing", "invalid_uuid"])
def test_jwt_rejects_invalid_tokens(kind):
    key = secrets.token_urlsafe(48)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "jti": str(uuid4()),
              "exp": datetime.now(UTC) + timedelta(minutes=10)}
    signing_key, algorithm = key, "HS256"
    if kind == "expired": claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    if kind == "forged": signing_key = secrets.token_urlsafe(48)
    if kind == "none": signing_key, algorithm = None, "none"
    if kind == "missing": del claims["exp"]
    if kind == "invalid_uuid": claims["session_id"] = "invalid"
    with pytest.raises(ValueError, match="Invalid access token"):
        decode_access(jwt.encode(claims, signing_key, algorithm=algorithm), key)


def test_jwt_minimal_claims_and_ttl():
    key, user, session = secrets.token_urlsafe(48), uuid4(), uuid4()
    token = access_token(user, session, key)
    assert decode_access(token, key) == (user, session)
    claims = jwt.decode(token, key, algorithms=["HS256"])
    assert set(claims) == {"sub", "session_id", "jti", "exp"}
    assert 590 <= claims["exp"] - datetime.now(UTC).timestamp() <= 600


@pytest.mark.parametrize("origins", ["*", "https://*.example.test", "https://app.example.test/path", "http://app.example.test", "https://user:secret@example.test", "https://example.test:invalid"])
def test_production_rejects_invalid_origins_without_breaking_settings(origins):
    settings = Settings(_env_file=None, ENVIRONMENT="production", PWA_ALLOWED_ORIGINS=origins)
    assert settings.allowed_pwa_origins() == ()
    with pytest.raises(HTTPException) as error:
        require_auth_config(settings)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_auth_config_does_not_break_production_startup(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "")
    monkeypatch.setenv("PWA_ALLOWED_ORIGINS", "")
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            result = await client.post("/api/v1/auth/login", json={"email":"user@example.test","password":"private"})
            assert result.status_code == 503
            assert "private" not in result.text
    get_settings.cache_clear()


def test_auth_migration_sql():
    path = PROJECT_ROOT / "alembic/versions/20260903_0006_pwa_auth.py"
    upgrade = render_migration_sql("upgrade", path).lower()
    downgrade = render_migration_sql("downgrade", path).lower()
    for table in ("users", "business_user_memberships", "auth_sessions"):
        assert f"create table {table}" in upgrade
        assert f"drop table {table}" in downgrade
    assert "refresh_token_hash" in upgrade and "argon2id" in upgrade
    assert "insert into" not in upgrade and "cascade" not in upgrade

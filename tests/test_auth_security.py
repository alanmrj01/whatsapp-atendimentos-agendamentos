import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.auth.dependencies import require_auth_config
from app.auth.password_email import BrevoPasswordResetMailer
from app.auth.security import (
    access_token,
    decode_access,
    hash_password,
    new_password_reset_token,
    token_hash,
    verify_password,
)
from app.auth.schemas import (
    AccessResponse,
    LoginRequest,
    MeResponse,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
)
from app.core.config import (
    PasswordRecoveryConfigurationError,
    PasswordResetEmailConfiguration,
    Settings,
    get_settings,
)
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
    reset_token = new_password_reset_token()
    assert len(reset_token) >= 32
    assert len(token_hash(reset_token)) == 64
    assert reset_token not in repr(
        PasswordResetConfirmRequest(token=reset_token, new_password=password)
    )
    assert password not in repr(
        PasswordChangeRequest(
            current_password=password,
            new_password=secrets.token_urlsafe(24),
        )
    )


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


def test_access_response_can_hydrate_session_without_a_second_request():
    user_id = uuid4()
    response = AccessResponse(
        access_token="opaque-test-token",
        session=MeResponse(
            id=user_id,
            email="member@example.test",
            platform_role=None,
            active_business_id=None,
            memberships=[],
        ),
    )

    assert response.session.id == user_id
    assert response.session.memberships == []


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


@pytest.mark.asyncio
async def test_pwa_cors_allows_idempotency_key_preflight(monkeypatch):
    origin = "https://alovia.example.test"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PWA_ALLOWED_ORIGINS", origin)
    get_settings.cache_clear()
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.options(
                "/api/v1/auth/signup",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,idempotency-key",
                },
            )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
        assert "idempotency-key" in allowed_headers
    finally:
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



def test_password_reset_email_configuration_is_explicit_and_origin_bound():
    disabled = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        PWA_ALLOWED_ORIGINS="https://app.example.test",
    )
    with pytest.raises(PasswordRecoveryConfigurationError):
        disabled.require_password_reset_email_configuration()

    configured = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        PWA_ALLOWED_ORIGINS="https://app.example.test",
        PASSWORD_RECOVERY_ENABLED=True,
        BREVO_API_KEY="xkeysib-test-secret",
        PASSWORD_RESET_FROM_EMAIL="no-reply@example.test",
        PASSWORD_RESET_FROM_NAME="Alovia",
        PASSWORD_RESET_PUBLIC_BASE_URL="https://app.example.test",
    )
    email = configured.require_password_reset_email_configuration()
    assert email.public_base_url == "https://app.example.test"
    assert email.api_key.get_secret_value() == "xkeysib-test-secret"
    assert email.from_email == "no-reply@example.test"
    assert email.from_name == "Alovia"

    wrong_origin = configured.model_copy(
        update={"password_reset_public_base_url": "https://evil.example.test"}
    )
    with pytest.raises(PasswordRecoveryConfigurationError):
        wrong_origin.require_password_reset_email_configuration()



@pytest.mark.asyncio
async def test_password_reset_email_keeps_token_in_fragment_and_is_idempotent(
    monkeypatch,
):
    from app.auth import password_email

    class Response:
        status_code = 201

    class Client:
        def __init__(self) -> None:
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            self.calls.append((url, headers, json))
            return Response()

    client = Client()
    monkeypatch.setattr(
        password_email.httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )
    reset_id = uuid4()
    token = new_password_reset_token()
    mailer = BrevoPasswordResetMailer(
        PasswordResetEmailConfiguration(
            api_key=SecretStr("xkeysib-test-secret"),
            from_email="no-reply@example.test",
            from_name="Alovia",
            public_base_url="https://app.example.test",
        )
    )

    await mailer.send(
        reset_id=reset_id,
        email="member@example.test",
        token=token,
    )

    assert len(client.calls) == 1
    url, headers, payload = client.calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert headers["api-key"] == "xkeysib-test-secret"
    assert payload["headers"]["idempotencyKey"] == str(reset_id)
    assert payload["sender"] == {
        "name": "Alovia",
        "email": "no-reply@example.test",
    }
    assert payload["to"] == [{"email": "member@example.test"}]
    assert "xkeysib-test-secret" not in repr(payload)
    assert f"/redefinir-senha#token={token}" in payload["textContent"]
    assert "?token=" not in payload["textContent"]
    assert f"/redefinir-senha#token={token}" in payload["htmlContent"]
    assert 'src="https://app.example.test/app-icon-192.png"' in payload["htmlContent"]
    assert "Redefina sua senha" in payload["htmlContent"]
    assert "Recuperação de acesso" in payload["htmlContent"]
    assert "30 minutos" in payload["htmlContent"]
    assert "uma única vez" in payload["htmlContent"]
    assert "Sua senha atual continuará a mesma." in payload["htmlContent"]
    assert "Equipe Alovia" in payload["htmlContent"]


@pytest.mark.asyncio
async def test_brevo_password_reset_does_not_retry_ambiguous_transport_failure(
    monkeypatch,
):
    from app.auth import password_email

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            raise password_email.httpx.ConnectError("synthetic transport failure")

    client = Client()
    monkeypatch.setattr(
        password_email.httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )
    mailer = BrevoPasswordResetMailer(
        PasswordResetEmailConfiguration(
            api_key=SecretStr("xkeysib-test-secret"),
            from_email="no-reply@example.test",
            from_name="Alovia",
            public_base_url="https://app.example.test",
        )
    )

    with pytest.raises(password_email.PasswordResetEmailError):
        await mailer.send(
            reset_id=uuid4(),
            email="member@example.test",
            token=new_password_reset_token(),
        )

    assert client.calls == 1

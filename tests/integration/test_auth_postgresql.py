from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
import pytest_asyncio
from pydantic import SecretStr
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.security import COOKIE_NAME, hash_password, token_hash
from app.core.config import get_settings
from app.core.database import get_db
from app.main import create_app
from app.models import (
    AuthSession,
    Business,
    BusinessAccess,
    BusinessUserMembership,
    BusinessWhatsAppConnection,
    User,
)
from app.whatsapp.administration import WhatsAppConnectionAdministrationService
from app.whatsapp.embedded_signup import MetaAuthorizedAssets, MetaEmbeddedSignupService
from app.whatsapp.onboarding import WhatsAppOnboardingService
from tests.integration.test_booking_postgresql import TEST_DATABASE_URL, _async_url, migrated_test_database

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado")]
assert migrated_test_database
ORIGIN = "https://pwa.example.test"


async def test_auth_migration_0005_0006_0005_0006():
    config = Config("alembic.ini")
    for direction, revision in [("downgrade", "20260902_0005"), ("upgrade", "20260903_0006"),
                                 ("downgrade", "20260902_0005"), ("upgrade", "20260903_0006")]:
        await asyncio.to_thread(getattr(command, direction), config, revision)
        engine = create_async_engine(_async_url(TEST_DATABASE_URL))
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == revision
            exists = await db.scalar(text("SELECT to_regclass('public.auth_sessions') IS NOT NULL"))
            assert exists == (revision == "20260903_0006")
        await engine.dispose()


@pytest_asyncio.fixture
async def auth_env(monkeypatch):
    key, password = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    monkeypatch.setenv("AUTH_JWT_SECRET", key)
    monkeypatch.setenv("PWA_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("META_APP_ID", "333333333333333")
    monkeypatch.setenv("META_EMBEDDED_SIGNUP_CONFIG_ID", "444444444444444")
    monkeypatch.setenv("META_EMBEDDED_SIGNUP_VERSION", "v4")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    engine = create_async_engine(_async_url(TEST_DATABASE_URL))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()
    async def db_override():
        async with factory() as session:
            yield session
    app.dependency_overrides[get_db] = db_override
    a, b = uuid4(), uuid4()
    ids = {name: uuid4() for name in ("owner", "admin", "attendant", "viewer", "multi", "super", "inactive")}
    async with factory() as db, db.begin():
        for model in (
            AuthSession,
            BusinessUserMembership,
            User,
            BusinessWhatsAppConnection,
            BusinessAccess,
            Business,
        ):
            await db.execute(delete(model))
        db.add_all([Business(id=a, name="Company A"), Business(id=b, name="Company B")])
        stored_hash = hash_password(password)
        db.add_all([User(id=user_id, email=f"{name}@example.test", password_hash=stored_hash,
            is_active=name != "inactive", platform_role="super_admin" if name == "super" else None)
            for name, user_id in ids.items()])
        await db.flush()
        db.add_all([BusinessUserMembership(user_id=ids[role], business_id=a, role=role)
                    for role in ("owner", "admin", "attendant", "viewer")])
        db.add_all([BusinessUserMembership(user_id=ids["multi"], business_id=a, role="owner"),
                    BusinessUserMembership(user_id=ids["multi"], business_id=b, role="viewer")])
        db.add_all([BusinessWhatsAppConnection(business_id=a, mode="coexistence", status="connected"),
                    BusinessWhatsAppConnection(business_id=b, mode="api_only", status="pending")])
    client = AsyncClient(transport=ASGITransport(app=app), base_url="https://api.example.test", headers={"Origin":ORIGIN})
    try:
        yield SimpleNamespace(client=client, factory=factory, app=app, key=key, password=password, a=a, b=b, ids=ids)
    finally:
        await client.aclose()
        await engine.dispose()
        get_settings.cache_clear()


async def login(env, name="owner"):
    result = await env.client.post("/api/v1/auth/login", json={"email":f" {name.upper()}@EXAMPLE.TEST ","password":env.password})
    assert result.status_code == 200
    env.client.headers["Authorization"] = "Bearer " + result.json()["access_token"]
    return result


async def test_login_cookie_hash_me_and_no_sensitive_logs(auth_env, caplog):
    env = auth_env
    caplog.set_level(logging.INFO)
    result = await login(env)
    cookie = env.client.cookies.get(COOKIE_NAME)
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=lax" in result.headers["set-cookie"]
    assert "Path=/api/v1/auth" in result.headers["set-cookie"]
    assert set(result.json()) == {"access_token", "token_type", "expires_in"}
    me = await env.client.get("/api/v1/me")
    assert me.json()["active_business_id"] == str(env.a)
    assert me.json()["memberships"][0]["role"] == "owner"
    assert me.headers["cache-control"] == "no-store"
    async with env.factory() as db:
        session = await db.scalar(select(AuthSession))
        assert session.refresh_token_hash == token_hash(cookie)
        user = await db.get(User, env.ids["owner"])
        assert user.password_hash.startswith("$argon2id$")
        private = (env.password, cookie, env.key, user.password_hash, session.refresh_token_hash, result.json()["access_token"])
    for value in private:
        assert value not in caplog.text and value not in me.text
    connection = await env.client.get("/api/v1/whatsapp/connection")
    assert connection.json() == {"status":"connected","mode":"coexistence"}
    assert "credential" not in connection.text and "phone" not in connection.text


async def test_generic_login_failure_inactive_and_extra_fields(auth_env):
    env = auth_env
    responses = []
    for name, password in [("owner", "wrong"), ("unknown", env.password), ("inactive", env.password)]:
        r = await env.client.post("/api/v1/auth/login", json={"email":f"{name}@example.test","password":password})
        assert r.status_code == 401
        responses.append(r.json())
    assert responses[0] == responses[1] == responses[2]
    r = await env.client.post("/api/v1/auth/login", json={"email":"owner@example.test","password":env.password,"role":"super_admin"})
    assert r.status_code == 422 and env.password not in r.text


async def test_production_cookie_is_secure_and_missing_origin_rejected(auth_env, monkeypatch):
    env = auth_env
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    result = await login(env)
    assert "Secure" in result.headers["set-cookie"]
    assert "HttpOnly" in result.headers["set-cookie"]
    del env.client.headers["Origin"]
    for path in ("login", "refresh", "logout"):
        result = await env.client.post(f"/api/v1/auth/{path}", json={})
        assert result.status_code == 403 and "set-cookie" not in result.headers


async def test_refresh_rotation_reuse_and_logout(auth_env):
    env = auth_env
    first = await login(env)
    old = env.client.cookies.get(COOKIE_NAME)
    second = await env.client.post("/api/v1/auth/refresh", json={})
    assert second.status_code == 200
    assert env.client.cookies.get(COOKIE_NAME) != old
    replay = await env.client.post("/api/v1/auth/refresh", json={}, headers={"Cookie":f"{COOKIE_NAME}={old}"})
    assert replay.status_code == 401 and "set-cookie" not in replay.headers
    assert (await env.client.post("/api/v1/auth/logout", json={})).status_code == 204
    env.client.headers["Authorization"] = "Bearer " + first.json()["access_token"]
    assert (await env.client.get("/api/v1/me")).status_code == 401
    assert (await env.client.post("/api/v1/auth/refresh", json={})).status_code == 401
    assert (await env.client.post("/api/v1/auth/logout", json={})).status_code == 204


async def test_concurrent_refresh_exactly_one_rotation(auth_env):
    env = auth_env
    await login(env)
    old = env.client.cookies.get(COOKIE_NAME)
    responses = await asyncio.gather(*[env.client.post("/api/v1/auth/refresh", json={},
        headers={"Cookie":f"{COOKIE_NAME}={old}"}) for _ in range(2)])
    assert sorted(r.status_code for r in responses) == [200,401]
    async with env.factory() as db:
        assert len((await db.scalars(select(AuthSession))).all()) == 1


@pytest.mark.parametrize("role", ["owner", "admin", "attendant", "viewer"])
async def test_roles_and_tenant_input_rejected(auth_env, role):
    env = auth_env
    await login(env, role)
    assert (await env.client.get("/api/v1/whatsapp/connection")).status_code == 200
    r = await env.client.post("/api/v1/whatsapp/onboarding/plan", json={"intent":"keep_whatsapp_business"})
    assert r.status_code == (200 if role in {"owner","admin"} else 403)
    if r.status_code == 200: assert r.json()["requested_mode"] == "coexistence"
    assert (await env.client.get(f"/api/v1/whatsapp/connection?business_id={env.b}")).status_code == 400
    assert (await env.client.post("/api/v1/auth/active-business",json={"business_id":str(env.b)})).status_code == 403
    assert (await env.client.get("/api/v1/me")).json()["active_business_id"] == str(env.a)


async def test_multiple_business_selection_is_persisted_and_rechecked(auth_env):
    env = auth_env
    await login(env, "multi")
    assert (await env.client.get("/api/v1/me")).json()["active_business_id"] is None
    assert (await env.client.get("/api/v1/whatsapp/connection")).status_code == 403
    r = await env.client.post("/api/v1/auth/active-business", json={"business_id":str(env.b)})
    assert r.status_code == 200 and r.json()["active_business_id"] == str(env.b)
    r = await env.client.get("/api/v1/whatsapp/connection")
    assert r.json() == {"status":"pending","mode":"api_only"}
    assert (await env.client.post("/api/v1/whatsapp/onboarding/plan", json={"intent":"keep_whatsapp_business"})).status_code == 403
    assert (await env.client.post("/api/v1/auth/refresh", json={})).status_code == 200
    assert (await env.client.get("/api/v1/me")).json()["active_business_id"] == str(env.b)
    async with env.factory() as db, db.begin():
        await db.execute(delete(BusinessUserMembership).where(BusinessUserMembership.user_id==env.ids["multi"], BusinessUserMembership.business_id==env.b))
    assert (await env.client.get("/api/v1/whatsapp/connection")).status_code == 403


async def test_super_admin_cannot_supply_arbitrary_business(auth_env):
    env = auth_env
    await login(env, "super")
    me = (await env.client.get("/api/v1/me")).json()
    assert me["platform_role"] == "super_admin" and me["memberships"] == []
    assert (await env.client.get("/api/v1/whatsapp/connection")).status_code == 403
    assert (await env.client.post("/api/v1/auth/active-business",json={"business_id":str(env.a)})).status_code == 403


async def test_plan_preserves_confirmation_and_no_mutation(auth_env):
    env = auth_env
    await login(env)
    for intent, confirmed, mode, ready in [
        ("keep_whatsapp_business",False,"coexistence",True),
        ("use_new_or_dedicated_number",False,"api_only",True),
        ("use_existing_number_platform_only",False,"api_only",False),
        ("use_existing_number_platform_only",True,"api_only",True),
    ]:
        r = await env.client.post("/api/v1/whatsapp/onboarding/plan",json={"intent":intent,"platform_only_impact_confirmed":confirmed})
        assert r.status_code == 200 and r.json()["requested_mode"] == mode and r.json()["ready_to_continue"] == ready
    for extra in ({"business_id":str(env.b)}, {"access_token":secrets.token_urlsafe(24)}, {"platform_only_impact_confirmed":"false"}):
        assert (await env.client.post("/api/v1/whatsapp/onboarding/plan", json={"intent":"keep_whatsapp_business",**extra})).status_code == 422
    assert (await env.client.get("/api/v1/whatsapp/connection")).json()["status"] == "connected"


async def test_embedded_signup_free_is_blocked_and_paid_can_start(auth_env):
    env = auth_env
    await login(env)
    async with env.factory() as db, db.begin():
        db.add(BusinessAccess(business_id=env.a, access_mode="free"))
    blocked = await env.client.post(
        "/api/v1/whatsapp/onboarding/embedded-signup/start", json={}
    )
    assert blocked.status_code == 402

    async with env.factory() as db, db.begin():
        access = await db.get(BusinessAccess, env.a)
        access.access_mode = "paid"
        await db.execute(
            delete(BusinessWhatsAppConnection).where(
                BusinessWhatsAppConnection.business_id == env.a
            )
        )
    started = await env.client.post(
        "/api/v1/whatsapp/onboarding/embedded-signup/start", json={}
    )
    assert started.status_code == 200
    assert started.json() == {
        "app_id": "333333333333333",
        "configuration_id": "444444444444444",
        "graph_version": "v23.0",
        "embedded_signup_version": "v4",
        "mode": "coexistence",
    }
    assert "secret" not in started.text.lower()
    assert "token" not in started.text.lower()


class _EmbeddedSignupGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def exchange_and_validate(self, authorization_code, **hints):
        self.calls += 1
        assert authorization_code.get_secret_value() == "short-lived-code"
        assert hints["waba_id_hint"] == "111111111111111"
        return MetaAuthorizedAssets(
            access_token=SecretStr("synthetic-business-token"),
            waba_id="111111111111111",
            phone_number_id="222222222222222",
            display_phone_number="+55 12 99999-1234",
        )

    async def subscribe_app(self, assets):
        return None

    async def aclose(self):
        self.closed = True


class _EmbeddedSignupStore:
    async def store(self, business_id, credential):
        assert credential.get_secret_value() == "synthetic-business-token"
        return (
            "projects/test-project/secrets/"
            f"alovia-whatsapp-{business_id.hex}/versions/1"
        )


async def test_embedded_signup_completion_is_tenant_scoped_and_secret_free(
    auth_env, monkeypatch, caplog
):
    from app.api import public_pwa

    env = auth_env
    await login(env)
    async with env.factory() as db, db.begin():
        await db.execute(
            delete(BusinessWhatsAppConnection).where(
                BusinessWhatsAppConnection.business_id == env.a
            )
        )
    gateway = _EmbeddedSignupGateway()

    def factory(session, configuration):
        return (
            MetaEmbeddedSignupService(
                WhatsAppOnboardingService(
                    WhatsAppConnectionAdministrationService(session)
                ),
                gateway,
                _EmbeddedSignupStore(),
                configuration.graph_version,
            ),
            gateway,
        )

    monkeypatch.setattr(public_pwa, "_embedded_signup_service", factory)
    caplog.set_level(logging.INFO)
    payload = {
        "authorization_code": "short-lived-code",
        "waba_id": "111111111111111",
        "phone_number_id": "222222222222222",
    }
    cross_tenant = await env.client.post(
        "/api/v1/whatsapp/onboarding/embedded-signup/complete",
        json={**payload, "business_id": str(env.b)},
    )
    assert cross_tenant.status_code == 422
    assert gateway.calls == 0

    completed = await env.client.post(
        "/api/v1/whatsapp/onboarding/embedded-signup/complete",
        json=payload,
    )
    assert completed.status_code == 200
    assert completed.json() == {
        "status": "connected",
        "mode": "coexistence",
        "display_phone_number": "•••• 1234",
    }
    assert gateway.closed is True
    async with env.factory() as db:
        connection = await db.scalar(
            select(BusinessWhatsAppConnection).where(
                BusinessWhatsAppConnection.business_id == env.a
            )
        )
        assert connection.status == "connected"
        assert connection.mode == "coexistence"
        assert connection.meta_waba_id == "111111111111111"
        assert connection.meta_phone_number_id == "222222222222222"
        assert connection.credential_secret_ref.endswith("/versions/1")
        columns = {
            column.name for column in BusinessWhatsAppConnection.__table__.columns
        }
        assert "access_token" not in columns
    for private in (
        "short-lived-code",
        "synthetic-business-token",
        "+55 12 99999-1234",
    ):
        assert private not in completed.text
        assert private not in caplog.text


async def test_connected_embedded_signup_is_not_overwritten(auth_env, monkeypatch):
    from app.api import public_pwa

    env = auth_env
    await login(env)
    gateway = _EmbeddedSignupGateway()
    monkeypatch.setattr(
        public_pwa,
        "_embedded_signup_service",
        lambda *_: pytest.fail("provider must not be called for connected account"),
    )
    result = await env.client.post(
        "/api/v1/whatsapp/onboarding/embedded-signup/complete",
        json={
            "authorization_code": "short-lived-code",
            "waba_id": "111111111111111",
        },
    )
    assert result.status_code == 409
    assert gateway.calls == 0


@pytest.mark.parametrize("state", ["expired", "revoked", "inactive"])
async def test_session_state_blocks_access_and_refresh(auth_env, state):
    env = auth_env
    await login(env)
    async with env.factory() as db, db.begin():
        session = await db.scalar(select(AuthSession))
        if state == "expired": session.expires_at = datetime.now(UTC)-timedelta(seconds=1)
        if state == "revoked": session.revoked_at = datetime.now(UTC)
        if state == "inactive": (await db.get(User, env.ids["owner"])).is_active = False
    assert (await env.client.get("/api/v1/me")).status_code == 401
    assert (await env.client.post("/api/v1/auth/refresh",json={})).status_code == 401


async def test_anonymous_forged_expired_origin_and_cors(auth_env):
    env = auth_env
    assert (await env.client.get("/api/v1/me")).status_code == 401
    first = await login(env)
    claims = jwt.decode(first.json()["access_token"], env.key, algorithms=["HS256"])
    for token in (jwt.encode(claims,secrets.token_urlsafe(48),algorithm="HS256"),
                  jwt.encode({**claims,"exp":datetime.now(UTC)-timedelta(seconds=1)},env.key,algorithm="HS256")):
        assert (await env.client.get("/api/v1/me",headers={"Authorization":"Bearer "+token})).status_code == 401
    for path in ("login","refresh","logout"):
        r = await env.client.post(f"/api/v1/auth/{path}", json={}, headers={"Origin":"https://untrusted.example.test"})
        assert r.status_code == 403 and "set-cookie" not in r.headers
        assert "access-control-allow-origin" not in r.headers
    good = await env.client.options("/api/v1/me",headers={"Access-Control-Request-Method":"GET","Access-Control-Request-Headers":"authorization"})
    assert good.headers["access-control-allow-origin"] == ORIGIN
    assert good.headers["access-control-allow-credentials"] == "true"
    for path in ("refresh","logout"):
        assert (await env.client.post(f"/api/v1/auth/{path}",json={"extra":"rejected"})).status_code == 422

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from pydantic import ValidationError

from app.api import public_pwa
from app.auth.schemas import (
    EmptyRequest,
    MembershipResponse,
    MembershipRole,
    MetaEmbeddedSignupCompleteRequest,
)
from app.core.config import MetaEmbeddedSignupConfiguration, Settings
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.credentials import (
    GoogleSecretManagerCredentialProvider,
    GoogleSecretManagerCredentialStore,
)
from app.whatsapp.embedded_signup import (
    MetaAuthorizedAssets,
    MetaEmbeddedSignupGateway,
    MetaEmbeddedSignupRejected,
    MetaEmbeddedSignupService,
    MetaEmbeddedSignupUnavailable,
)

BUSINESS_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WABA_ID = "111111111111111"
PHONE_ID = "222222222222222"
RAW_TOKEN = "synthetic-token-value-for-tests"
RAW_CODE = "synthetic-authorization-code-for-tests"


def configuration() -> MetaEmbeddedSignupConfiguration:
    return MetaEmbeddedSignupConfiguration(
        app_id="333333333333333",
        configuration_id="444444444444444",
        graph_version="v23.0",
        embedded_signup_version="v4",
        app_secret=SecretStr("synthetic-app-secret-for-tests"),
        gcp_project_id="test-project",
    )


def settings() -> Settings:
    return Settings(
        _env_file=None,
        ENVIRONMENT="test",
        META_APP_ID="333333333333333",
        META_EMBEDDED_SIGNUP_CONFIG_ID="444444444444444",
        META_EMBEDDED_SIGNUP_VERSION="v4",
        META_GRAPH_VERSION="v23.0",
        META_APP_SECRET="synthetic-app-secret-for-tests",
        GCP_PROJECT_ID="test-project",
    )


class FakePrincipal:
    def __init__(self, access_mode: str = "paid") -> None:
        self.membership = MembershipResponse(
            business_id=BUSINESS_ID,
            business_name="Company A",
            role=MembershipRole.OWNER,
            access_mode=access_mode,
        )

    def active_membership(self):
        return self.membership


class EmptyAdministration:
    async def get_connection(self, business_id, *, for_update=False):
        assert business_id == BUSINESS_ID
        return None


@pytest.mark.asyncio
async def test_paid_business_can_start_and_free_is_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: EmptyAdministration(),
    )
    started = await public_pwa.start_meta_embedded_signup(
        EmptyRequest(), FakePrincipal(), settings(), object()
    )
    assert started.model_dump() == {
        "app_id": "333333333333333",
        "configuration_id": "444444444444444",
        "graph_version": "v23.0",
        "embedded_signup_version": "v4",
        "mode": "coexistence",
    }
    assert "secret" not in started.model_dump()

    with pytest.raises(HTTPException) as blocked:
        await public_pwa.start_meta_embedded_signup(
            EmptyRequest(), FakePrincipal("free"), settings(), object()
        )
    assert blocked.value.status_code == 402


def graph_transport(*, waba_id: str = WABA_ID, phone_id: str = PHONE_ID):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": RAW_TOKEN})
        if request.url.path.endswith(f"/{WABA_ID}"):
            return httpx.Response(200, json={"id": waba_id})
        if request.url.path.endswith(f"/{WABA_ID}/phone_numbers"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": phone_id,
                            "display_phone_number": "+55 12 99999-1234",
                        }
                    ]
                },
            )
        if request.url.path.endswith(f"/{WABA_ID}/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return httpx.MockTransport(handler), calls


@pytest.mark.asyncio
async def test_graph_exchange_validates_assets_and_subscribes_without_logging_secrets(
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport, calls = graph_transport()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v23.0/",
    ) as client:
        gateway = MetaEmbeddedSignupGateway(configuration(), client=client)
        assets = await gateway.exchange_and_validate(
            SecretStr(RAW_CODE),
            waba_id_hint=WABA_ID,
            phone_number_id_hint=PHONE_ID,
        )
        await gateway.subscribe_app(assets)

    assert assets.waba_id == WABA_ID
    assert assets.phone_number_id == PHONE_ID
    assert assets.access_token.get_secret_value() == RAW_TOKEN
    assert calls == [
        ("GET", "/v23.0/oauth/access_token"),
        ("GET", f"/v23.0/{WABA_ID}"),
        ("GET", f"/v23.0/{WABA_ID}/phone_numbers"),
        ("POST", f"/v23.0/{WABA_ID}/subscribed_apps"),
    ]
    assert RAW_TOKEN not in caplog.text
    assert RAW_CODE not in caplog.text
    assert configuration().app_secret.get_secret_value() not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_waba", "returned_phone"),
    [("999999999999999", PHONE_ID), (WABA_ID, "999999999999999")],
)
async def test_graph_rejects_unconfirmed_waba_or_phone(
    returned_waba: str,
    returned_phone: str,
) -> None:
    transport, _ = graph_transport(
        waba_id=returned_waba,
        phone_id=returned_phone,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v23.0/",
    ) as client:
        gateway = MetaEmbeddedSignupGateway(configuration(), client=client)
        with pytest.raises(MetaEmbeddedSignupRejected):
            await gateway.exchange_and_validate(
                SecretStr(RAW_CODE),
                waba_id_hint=WABA_ID,
                phone_number_id_hint=PHONE_ID,
            )


@pytest.mark.asyncio
async def test_invalid_meta_payload_is_rejected_without_provider_details() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"not-json")
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v23.0/",
    ) as client:
        gateway = MetaEmbeddedSignupGateway(configuration(), client=client)
        with pytest.raises(
            MetaEmbeddedSignupUnavailable,
            match="Meta response is invalid",
        ):
            await gateway.exchange_and_validate(
                SecretStr(RAW_CODE),
                waba_id_hint=WABA_ID,
                phone_number_id_hint=PHONE_ID,
            )


@pytest.mark.parametrize(
    "forbidden_field",
    ["business_id", "credential_secret_ref", "provider_confirmed", "access_token"],
)
def test_public_completion_contract_rejects_server_controlled_fields(
    forbidden_field: str,
) -> None:
    payload = {
        "authorization_code": RAW_CODE,
        "waba_id": WABA_ID,
        forbidden_field: "untrusted-client-value",
    }
    with pytest.raises(ValidationError):
        MetaEmbeddedSignupCompleteRequest.model_validate(payload)


class FakeSecretManagerClient:
    def __init__(self) -> None:
        self.created = 0
        self.added_payload: bytes | None = None

    def create_secret(self, *, request):
        self.created += 1
        return SimpleNamespace(name="created")

    def add_secret_version(self, *, request):
        self.added_payload = request["payload"]["data"]
        return SimpleNamespace(
            name=(
                "projects/test-project/secrets/"
                f"alovia-whatsapp-{BUSINESS_ID.hex}/versions/1"
            )
        )

    def access_secret_version(self, *, request):
        return SimpleNamespace(payload=SimpleNamespace(data=RAW_TOKEN.encode()))


@pytest.mark.asyncio
async def test_secret_manager_stores_and_resolves_only_version_reference() -> None:
    client = FakeSecretManagerClient()
    store = GoogleSecretManagerCredentialStore("test-project", client=client)
    reference = await store.store(BUSINESS_ID, SecretStr(RAW_TOKEN))
    assert reference.endswith("/versions/1")
    assert RAW_TOKEN not in reference
    assert client.added_payload == RAW_TOKEN.encode()

    connection = SimpleNamespace(credential_secret_ref=reference)
    resolved = await GoogleSecretManagerCredentialProvider(client).resolve(connection)
    assert resolved.get_secret_value() == RAW_TOKEN


class FakeGateway:
    def __init__(self) -> None:
        self.subscribed = False

    async def exchange_and_validate(self, authorization_code, **hints):
        assert authorization_code.get_secret_value() == RAW_CODE
        assert hints == {
            "waba_id_hint": WABA_ID,
            "phone_number_id_hint": PHONE_ID,
        }
        return MetaAuthorizedAssets(
            access_token=SecretStr(RAW_TOKEN),
            waba_id=WABA_ID,
            phone_number_id=PHONE_ID,
            display_phone_number="+55 12 99999-1234",
        )

    async def subscribe_app(self, assets):
        self.subscribed = True


class FakeStore:
    def __init__(self) -> None:
        self.business_id: uuid.UUID | None = None

    async def store(self, business_id, credential):
        self.business_id = business_id
        assert credential.get_secret_value() == RAW_TOKEN
        return "projects/test-project/secrets/business-token/versions/1"


class FakeOnboarding:
    def __init__(self) -> None:
        self.business_id: uuid.UUID | None = None
        self.completion = None

    async def complete_provider_onboarding(self, business_id, completion):
        self.business_id = business_id
        self.completion = completion
        return SimpleNamespace(
            status=WhatsAppConnectionStatus.CONNECTED,
            mode=WhatsAppConnectionMode.COEXISTENCE,
        )


@pytest.mark.asyncio
async def test_valid_coexistence_completion_is_scoped_and_db_receives_no_token() -> None:
    gateway = FakeGateway()
    store = FakeStore()
    onboarding = FakeOnboarding()
    service = MetaEmbeddedSignupService(
        onboarding, gateway, store, "v23.0"
    )
    result = await service.complete_coexistence(
        BUSINESS_ID,
        SecretStr(RAW_CODE),
        waba_id_hint=WABA_ID,
        phone_number_id_hint=PHONE_ID,
    )

    assert result.status is WhatsAppConnectionStatus.CONNECTED
    assert gateway.subscribed is True
    assert store.business_id == BUSINESS_ID
    assert onboarding.business_id == BUSINESS_ID
    assert onboarding.completion.confirmed_mode is WhatsAppConnectionMode.COEXISTENCE
    assert onboarding.completion.provider_confirmed is True
    assert RAW_TOKEN not in repr(onboarding.completion)

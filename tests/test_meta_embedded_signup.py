from __future__ import annotations

import json
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
    MetaApiOnlyEmbeddedSignupCompleteRequest,
    MetaApiOnlyEmbeddedSignupStartRequest,
    MetaEmbeddedSignupAssetsRequest,
    MetaEmbeddedSignupCompleteRequest,
    MetaEmbeddedSignupTelemetryRequest,
)
from app.core.config import MetaEmbeddedSignupConfiguration, Settings
from app.core.logging import JsonFormatter
from app.whatsapp.administration import (
    META_ONBOARDING_PENDING,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus
from app.whatsapp.onboarding import WhatsAppOnboardingIntent
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
        graph_version="v25.0",
        embedded_signup_version="v4",
        app_secret=SecretStr("synthetic-app-secret-for-tests"),
        gcp_project_id="test-project",
    )


def settings(*, api_only_enabled: bool = False, embedded_signup_version: str = "v4") -> Settings:
    return Settings(
        _env_file=None,
        ENVIRONMENT="test",
        META_APP_ID="333333333333333",
        META_EMBEDDED_SIGNUP_CONFIG_ID="444444444444444",
        META_EMBEDDED_SIGNUP_VERSION=embedded_signup_version,
        META_GRAPH_VERSION="v25.0",
        META_APP_SECRET="synthetic-app-secret-for-tests",
        GCP_PROJECT_ID="test-project",
        WHATSAPP_API_ONLY_FALLBACK_ENABLED=api_only_enabled,
    )


class FakePrincipal:
    def __init__(
        self,
        access_mode: str = "paid",
        role: MembershipRole = MembershipRole.OWNER,
    ) -> None:
        self.membership = MembershipResponse(
            business_id=BUSINESS_ID,
            business_name="Company A",
            role=role,
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
        "graph_version": "v25.0",
        "embedded_signup_version": "v4",
        "mode": "coexistence",
    }
    assert "secret" not in started.model_dump()

    with pytest.raises(HTTPException) as blocked:
        await public_pwa.start_meta_embedded_signup(
            EmptyRequest(), FakePrincipal("free"), settings(), object()
        )
    assert blocked.value.status_code == 402


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class PendingAdministration(EmptyAdministration):
    def __init__(self) -> None:
        self.assets: tuple[str, str | None, str] | None = None

    async def begin_pending_connection(self, business_id, mode):
        assert business_id == BUSINESS_ID
        assert mode is WhatsAppConnectionMode.COEXISTENCE
        return SimpleNamespace(
            status=WhatsAppConnectionStatus.PENDING,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=META_ONBOARDING_PENDING,
        )

    async def record_pending_meta_assets(
        self,
        business_id,
        *,
        meta_waba_id,
        meta_phone_number_id,
        graph_version,
    ):
        assert business_id == BUSINESS_ID
        self.assets = (meta_waba_id, meta_phone_number_id, graph_version)


@pytest.mark.asyncio
async def test_begin_attempt_and_assets_are_persisted_before_final_completion(
    monkeypatch,
) -> None:
    administration = PendingAdministration()
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: administration,
    )
    db = FakeDb()

    pending = await public_pwa.begin_meta_embedded_signup_attempt(
        EmptyRequest(),
        FakePrincipal(),
        settings(),
        db,
    )
    assert pending.status == "pending"
    assert pending.mode == "coexistence"
    assert pending.pending_state == "authorization_pending"
    assert db.commits == 1

    response = await public_pwa.record_meta_embedded_signup_assets(
        MetaEmbeddedSignupAssetsRequest(
            waba_id=WABA_ID,
            phone_number_id=PHONE_ID,
        ),
        FakePrincipal(),
        settings(),
        db,
    )
    assert response.status_code == 204
    assert administration.assets == (WABA_ID, PHONE_ID, "v25.0")
    assert db.commits == 2


def test_public_connection_distinguishes_meta_review_from_started_authorization() -> None:
    review_pending = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.PENDING,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=META_ONBOARDING_PENDING,
            has_phone_number_id=True,
            masked_display_phone_number=None,
            meta_review_status=None,
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert review_pending.pending_state == "meta_review_pending"
    assert review_pending.journey_state == "meta_review_pending"
    assert review_pending.requires_user_action is False
    assert review_pending.next_action == "wait_for_meta_review"

    authorization_pending = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.PENDING,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=META_ONBOARDING_PENDING,
            has_phone_number_id=False,
            masked_display_phone_number=None,
            meta_review_status=None,
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert authorization_pending.pending_state == "authorization_pending"
    assert authorization_pending.journey_state == "authorization_pending"
    assert authorization_pending.requires_user_action is True
    assert authorization_pending.next_action == "continue_authorization"

    review_rejected = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.PENDING,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=META_ONBOARDING_PENDING,
            has_phone_number_id=True,
            masked_display_phone_number=None,
            meta_review_status="rejected",
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert review_rejected.pending_state == "meta_review_rejected"
    assert review_rejected.journey_state == "meta_review_rejected"
    assert review_rejected.requires_user_action is True
    assert review_rejected.next_action == "review_meta_rejection"

    disconnected = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.DISCONNECTED,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=None,
            has_phone_number_id=False,
            masked_display_phone_number=None,
            meta_review_status=None,
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert disconnected.journey_state == "not_started"
    assert disconnected.requires_user_action is True
    assert disconnected.next_action == "choose_mode"

    connected = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.CONNECTED,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code=None,
            has_phone_number_id=True,
            masked_display_phone_number="(**) *****-1234",
            meta_review_status="approved",
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert connected.journey_state == "connected"
    assert connected.requires_user_action is False
    assert connected.next_action == "none"

    errored = public_pwa._public_connection(
        SimpleNamespace(
            status=WhatsAppConnectionStatus.ERROR,
            mode=WhatsAppConnectionMode.COEXISTENCE,
            last_error_code="META_AUTHORIZATION_FAILED",
            has_phone_number_id=False,
            masked_display_phone_number=None,
            meta_review_status=None,
            preferred_mode=None,
            mode_switch_requested_at=None,
            mode_switch_last_checked_at=None,
            mode_switch_next_check_at=None,
        )
    )
    assert errored.journey_state == "error"
    assert errored.requires_user_action is True
    assert errored.next_action == "resolve_connection"


@pytest.mark.asyncio
async def test_only_paid_owner_and_admin_can_start_embedded_signup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: EmptyAdministration(),
    )
    for role in (MembershipRole.OWNER, MembershipRole.ADMIN):
        started = await public_pwa.start_meta_embedded_signup(
            EmptyRequest(), FakePrincipal(role=role), settings(), object()
        )
        assert started.mode == "coexistence"

    for role in (MembershipRole.ATTENDANT, MembershipRole.VIEWER):
        with pytest.raises(HTTPException) as blocked:
            await public_pwa.start_meta_embedded_signup(
                EmptyRequest(), FakePrincipal(role=role), settings(), object()
            )
        assert blocked.value.status_code == 403


@pytest.mark.asyncio
async def test_client_telemetry_is_sanitized_and_access_controlled(caplog) -> None:
    caplog.set_level(logging.INFO)
    payload = MetaEmbeddedSignupTelemetryRequest(
        stage="login_callback_received",
        authorization_code_received=False,
        waba_id_received=True,
    )

    response = await public_pwa.meta_embedded_signup_telemetry(
        payload, FakePrincipal()
    )

    assert response.status_code == 204
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "meta_embedded_signup_client_progress"
    )
    assert record.stage == "login_callback_received"
    assert record.authorization_code_received is False
    assert record.waba_id_received is True
    assert RAW_CODE not in caplog.text
    assert RAW_TOKEN not in caplog.text

    with pytest.raises(HTTPException) as free:
        await public_pwa.meta_embedded_signup_telemetry(
            payload, FakePrincipal("free")
        )
    assert free.value.status_code == 402

    with pytest.raises(HTTPException) as read_only:
        await public_pwa.meta_embedded_signup_telemetry(
            payload, FakePrincipal(role=MembershipRole.VIEWER)
        )
    assert read_only.value.status_code == 403


def test_json_formatter_preserves_only_safe_meta_onboarding_fields() -> None:
    record = logging.LogRecord(
        name="app.api.public_pwa",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="meta_embedded_signup_client_progress",
        args=(),
        exc_info=None,
    )
    record.stage = "login_callback_received"
    record.authorization_code_received = True
    record.waba_id_received = True
    record.phone_number_id_received = False
    record.intermediate_step_received = True
    record.review_decision = "APPROVED"
    record.authorization_code = RAW_CODE
    record.access_token = RAW_TOKEN

    payload = json.loads(JsonFormatter().format(record))

    assert payload["stage"] == "login_callback_received"
    assert payload["authorization_code_received"] is True
    assert payload["waba_id_received"] is True
    assert payload["phone_number_id_received"] is False
    assert payload["intermediate_step_received"] is True
    assert payload["review_decision"] == "APPROVED"
    assert RAW_CODE not in json.dumps(payload)
    assert RAW_TOKEN not in json.dumps(payload)


def test_client_telemetry_contract_rejects_unknown_or_sensitive_fields() -> None:
    with pytest.raises(ValidationError):
        MetaEmbeddedSignupTelemetryRequest(
            stage="unknown",
        )
    for sensitive in ("authorization_code", "access_token", "app_secret"):
        with pytest.raises(ValidationError):
            MetaEmbeddedSignupTelemetryRequest.model_validate(
                {"stage": "sdk_ready", sensitive: "private"}
            )


class FlushOnlySession:
    async def flush(self) -> None:
        return None


class ReviewRepository:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def get_active_connection_by_waba_id(
        self,
        meta_waba_id,
        *,
        for_update=False,
    ):
        assert meta_waba_id == WABA_ID
        assert for_update is True
        return self.connection


@pytest.mark.asyncio
async def test_review_decision_never_changes_connection_lifecycle() -> None:
    connection = SimpleNamespace(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        business_id=BUSINESS_ID,
        provider="meta",
        status=WhatsAppConnectionStatus.CONNECTED.value,
        mode=WhatsAppConnectionMode.COEXISTENCE.value,
        meta_phone_number_id=PHONE_ID,
        credential_secret_ref="projects/test-project/secrets/token/versions/1",
        connected_at=None,
        disconnected_at=None,
        display_phone_number="+55 12 99999-1234",
        last_error_code=None,
        meta_review_status=None,
        preferred_mode=None,
        mode_switch_requested_at=None,
        mode_switch_last_checked_at=None,
        mode_switch_next_check_at=None,
    )
    administration = WhatsAppConnectionAdministrationService(
        FlushOnlySession()
    )
    administration._repository = ReviewRepository(connection)

    approved = await administration.record_meta_review_decision(
        WABA_ID,
        "APPROVED",
    )
    assert approved is not None
    assert connection.status == WhatsAppConnectionStatus.CONNECTED.value
    assert connection.last_error_code is None
    assert connection.meta_review_status == "approved"

    rejected = await administration.record_meta_review_decision(
        WABA_ID,
        "REJECTED",
    )
    assert rejected is not None
    assert connection.status == WhatsAppConnectionStatus.CONNECTED.value
    assert connection.last_error_code is None
    assert connection.meta_review_status == "rejected"


def graph_transport(*, waba_id: str = WABA_ID, phone_id: str = PHONE_ID):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
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
async def test_graph_deregisters_phone_only_after_explicit_server_call() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith(
            f"/{PHONE_ID}/deregister"
        ):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        await MetaEmbeddedSignupGateway(
            configuration(), client=client
        ).deregister_phone(
            PHONE_ID,
            SecretStr(RAW_TOKEN),
        )

    assert seen == [("POST", f"/v25.0/{PHONE_ID}/deregister")]


@pytest.mark.asyncio
async def test_graph_reads_account_review_status_without_changing_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/{WABA_ID}"):
            return httpx.Response(
                200,
                json={
                    "id": WABA_ID,
                    "account_review_status": "APPROVED",
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        status_value = await MetaEmbeddedSignupGateway(
            configuration(), client=client
        ).fetch_account_review_status(
            WABA_ID,
            SecretStr(RAW_TOKEN),
        )

    assert status_value == "approved"


@pytest.mark.asyncio
async def test_graph_exchange_validates_assets_and_subscribes_without_logging_secrets(
    caplog,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport, calls = graph_transport()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v25.0/",
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
    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/v25.0/oauth/access_token"),
        ("GET", f"/v25.0/{WABA_ID}"),
        ("GET", f"/v25.0/{WABA_ID}/phone_numbers"),
        ("POST", f"/v25.0/{WABA_ID}/subscribed_apps"),
    ]
    assert calls[0].url.params["redirect_uri"] == ""
    assert calls[0].url.params["client_id"] == configuration().app_id
    assert RAW_TOKEN not in caplog.text
    assert RAW_CODE not in caplog.text
    assert configuration().app_secret.get_secret_value() not in caplog.text
    assert {
        "token_exchange_ok",
        "waba_validation_ok",
        "phone_validation_ok",
        "subscription_ok",
    }.issubset({record.stage for record in caplog.records if hasattr(record, "stage")})


@pytest.mark.asyncio
async def test_graph_resolves_the_only_phone_when_session_info_omits_it() -> None:
    transport, _ = graph_transport()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        assets = await MetaEmbeddedSignupGateway(
            configuration(), client=client
        ).exchange_and_validate(
            SecretStr(RAW_CODE),
            waba_id_hint=WABA_ID,
            phone_number_id_hint=None,
        )

    assert assets.phone_number_id == PHONE_ID


@pytest.mark.asyncio
async def test_graph_rejects_ambiguous_phones_when_session_info_omits_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": RAW_TOKEN})
        if request.url.path.endswith(f"/{WABA_ID}"):
            return httpx.Response(200, json={"id": WABA_ID})
        if request.url.path.endswith(f"/{WABA_ID}/phone_numbers"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": PHONE_ID},
                        {"id": "555555555555555"},
                    ]
                },
            )
        raise AssertionError("unexpected Graph request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        with pytest.raises(
            MetaEmbeddedSignupRejected,
            match="Meta phone number selection is ambiguous",
        ):
            await MetaEmbeddedSignupGateway(
                configuration(), client=client
            ).exchange_and_validate(
                SecretStr(RAW_CODE),
                waba_id_hint=WABA_ID,
                phone_number_id_hint=None,
            )


@pytest.mark.asyncio
async def test_realistic_graph_error_is_sanitized_and_logs_no_secrets(caplog) -> None:
    caplog.set_level(logging.INFO)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            400,
            json={
                "error": {
                    "message": f"invalid {RAW_CODE} {RAW_TOKEN}",
                    "type": "OAuthException",
                    "code": 100,
                }
            },
        )
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        with pytest.raises(
            MetaEmbeddedSignupRejected,
            match="Meta authorization was rejected",
        ) as rejected:
            await MetaEmbeddedSignupGateway(
                configuration(), client=client
            ).exchange_and_validate(
                SecretStr(RAW_CODE),
                waba_id_hint=WABA_ID,
                phone_number_id_hint=PHONE_ID,
            )

    assert RAW_CODE not in str(rejected.value)
    assert RAW_TOKEN not in str(rejected.value)
    assert RAW_CODE not in caplog.text
    assert RAW_TOKEN not in caplog.text


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
        base_url="https://graph.facebook.com/v25.0/",
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
        base_url="https://graph.facebook.com/v25.0/",
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
    [
        "business_id",
        "credential_secret_ref",
        "provider_confirmed",
        "access_token",
        "app_secret",
        "token",
    ],
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
        self.registered_pin: str | None = None

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

    async def register_phone(self, assets, registration_pin):
        self.registered_pin = registration_pin.get_secret_value()


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
            mode=completion.confirmed_mode,
        )


@pytest.mark.asyncio
async def test_valid_coexistence_completion_is_scoped_and_db_receives_no_token(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    gateway = FakeGateway()
    store = FakeStore()
    onboarding = FakeOnboarding()
    service = MetaEmbeddedSignupService(
        onboarding, gateway, store, "v25.0"
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
    assert {"secret_store_ok", "connection_saved"}.issubset(
        {record.stage for record in caplog.records if hasattr(record, "stage")}
    )
    assert RAW_TOKEN not in caplog.text
    assert RAW_CODE not in caplog.text


@pytest.mark.asyncio
async def test_api_only_start_is_fail_closed_until_feature_flag_is_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: EmptyAdministration(),
    )
    payload = MetaApiOnlyEmbeddedSignupStartRequest(
        intent=WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER,
    )

    with pytest.raises(HTTPException) as blocked:
        await public_pwa.start_meta_api_only_signup(
            payload, FakePrincipal(), settings(), object()
        )

    assert blocked.value.status_code == 503


@pytest.mark.asyncio
async def test_api_only_start_requires_v4_even_when_feature_is_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: EmptyAdministration(),
    )
    payload = MetaApiOnlyEmbeddedSignupStartRequest(
        intent=WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER,
    )
    with pytest.raises(HTTPException) as blocked:
        await public_pwa.start_meta_api_only_signup(
            payload,
            FakePrincipal(),
            settings(api_only_enabled=True, embedded_signup_version="v3"),
            object(),
        )
    assert blocked.value.status_code == 503


@pytest.mark.asyncio
async def test_api_only_start_requires_explicit_confirmation_for_existing_number(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_pwa,
        "WhatsAppConnectionAdministrationService",
        lambda _: EmptyAdministration(),
    )
    unconfirmed = MetaApiOnlyEmbeddedSignupStartRequest(
        intent=WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY,
        platform_only_impact_confirmed=False,
    )
    with pytest.raises(HTTPException) as blocked:
        await public_pwa.start_meta_api_only_signup(
            unconfirmed,
            FakePrincipal(),
            settings(api_only_enabled=True),
            object(),
        )
    assert blocked.value.status_code == 409

    confirmed = MetaApiOnlyEmbeddedSignupStartRequest(
        intent=WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY,
        platform_only_impact_confirmed=True,
    )
    started = await public_pwa.start_meta_api_only_signup(
        confirmed,
        FakePrincipal(),
        settings(api_only_enabled=True),
        object(),
    )
    assert started.mode == "api_only"
    assert (
        started.intent
        is WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY
    )


def test_api_only_contract_rejects_coexistence_intent_and_invalid_pin() -> None:
    with pytest.raises(ValidationError):
        MetaApiOnlyEmbeddedSignupStartRequest(
            intent=WhatsAppOnboardingIntent.KEEP_WHATSAPP_BUSINESS,
        )
    with pytest.raises(ValidationError):
        MetaApiOnlyEmbeddedSignupCompleteRequest(
            intent=WhatsAppOnboardingIntent.USE_NEW_OR_DEDICATED_NUMBER,
            authorization_code=RAW_CODE,
            waba_id=WABA_ID,
            phone_number_id=PHONE_ID,
            registration_pin=SecretStr("12A456"),
        )


@pytest.mark.asyncio
async def test_graph_registers_api_only_phone_without_logging_pin(caplog) -> None:
    pin = "847291"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(f"/{PHONE_ID}/register"):
            return httpx.Response(200, json={"success": True})
        raise AssertionError("unexpected Graph request")

    assets = MetaAuthorizedAssets(
        access_token=SecretStr(RAW_TOKEN),
        waba_id=WABA_ID,
        phone_number_id=PHONE_ID,
        display_phone_number="+55 12 99999-1234",
    )
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://graph.facebook.com/v25.0/",
    ) as client:
        gateway = MetaEmbeddedSignupGateway(configuration(), client=client)
        await gateway.register_phone(assets, SecretStr(pin))

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == f"/v25.0/{PHONE_ID}/register"
    assert requests[0].read().decode() == (
        '{"messaging_product":"whatsapp","pin":"847291"}'
    )
    assert pin not in caplog.text
    assert RAW_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_api_only_completion_registers_before_marking_connection_connected(
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    gateway = FakeGateway()
    store = FakeStore()
    onboarding = FakeOnboarding()
    service = MetaEmbeddedSignupService(
        onboarding, gateway, store, "v25.0"
    )
    result = await service.complete_api_only(
        BUSINESS_ID,
        SecretStr(RAW_CODE),
        intent=WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY,
        platform_only_impact_confirmed=True,
        registration_pin=SecretStr("847291"),
        waba_id_hint=WABA_ID,
        phone_number_id_hint=PHONE_ID,
    )

    assert result.status is WhatsAppConnectionStatus.CONNECTED
    assert result.mode is WhatsAppConnectionMode.API_ONLY
    assert gateway.subscribed is True
    assert gateway.registered_pin == "847291"
    assert onboarding.completion.confirmed_mode is WhatsAppConnectionMode.API_ONLY
    assert (
        onboarding.completion.intent
        is WhatsAppOnboardingIntent.USE_EXISTING_NUMBER_PLATFORM_ONLY
    )
    assert onboarding.completion.platform_only_impact_confirmed is True
    assert RAW_TOKEN not in repr(onboarding.completion)
    assert "847291" not in repr(onboarding.completion)
    assert "847291" not in caplog.text

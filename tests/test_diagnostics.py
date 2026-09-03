import asyncio
import logging
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import ProgrammingError

from app.core.config import Settings, get_settings
from app.diagnostics import service as diagnostic_service
from app.diagnostics.dependencies import get_diagnostics_service, require_diagnostics_oidc
from app.diagnostics.models import (
    ComponentResult, DatabaseDetails, DiagnosticCode as Code,
    DiagnosticStatus as Status, EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import DiagnosticsService, migration_result
from app.main import app
from app.tasks import auth
from tests.diagnostics_fakes import FakeDiagnosticsRepository


def settings() -> Settings:
    return Settings(_env_file=None).model_copy(update={
        "environment": "production", "cloud_tasks_enabled": False,
        "outbound_tasks_enabled": False, "diagnostics_oidc_audience": None,
        "diagnostics_invoker_email": None,
        "cloud_tasks_oidc_audience": "https://test.example.run.app",
        "cloud_tasks_invoker_email": "diagnostic@test.iam.gserviceaccount.com",
        "app_commit_sha": "a" * 40,
    })


@pytest.mark.parametrize(("values", "code", "status"), [
    ([EXPECTED_SCHEMA_REVISION], Code.MIGRATION_OK, Status.OK),
    (["20260902_0004"], Code.MIGRATION_BEHIND, Status.ERROR),
    (["20260901_0001"], Code.MIGRATION_BEHIND, Status.ERROR),
    (["20260903_0006"], Code.MIGRATION_AHEAD, Status.ERROR),
    (["untrusted-secret-value"], Code.MIGRATION_UNKNOWN, Status.UNKNOWN),
    (["20260831_0099"], Code.MIGRATION_UNKNOWN, Status.UNKNOWN),
    ([], Code.MIGRATION_UNKNOWN, Status.UNKNOWN),
    ([EXPECTED_SCHEMA_REVISION, "20260902_0004"], Code.MIGRATION_UNKNOWN, Status.UNKNOWN),
])
def test_revision_comparison_is_fail_closed_and_sanitized(values, code, status):
    result = migration_result(values, 1)
    assert result.code is code
    assert result.status is status
    assert "untrusted-secret-value" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "code"), [
    (OSError("sensitive-host"), Code.DB_UNREACHABLE),
    (TimeoutError("sensitive-password"), Code.DB_TIMEOUT),
    (ProgrammingError("sensitive SQL", {}, Exception("sensitive-token")), Code.DB_QUERY_FAILED),
    (RuntimeError("sensitive-message"), Code.UNKNOWN_COMPONENT_ERROR),
])
async def test_db_error_classification_and_no_sensitive_output(error, code, caplog):
    repo = FakeDiagnosticsRepository()
    repo.ping_error = error
    caplog.set_level(logging.DEBUG)
    report = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert report.status is Status.ERROR
    assert report.components.database.code is code
    assert repo.calls == ["ping"]
    assert "sensitive" not in report.model_dump_json() + caplog.text


@pytest.mark.asyncio
async def test_probe_has_short_timeout(monkeypatch):
    repo = FakeDiagnosticsRepository()

    async def slow_ping():
        await asyncio.sleep(10)

    monkeypatch.setattr(repo, "ping", slow_ping)
    monkeypatch.setattr(diagnostic_service, "QUERY_TIMEOUT_SECONDS", .01)
    result = await DiagnosticsService(repo, settings()).readiness(True)
    assert result.database.code is Code.DB_TIMEOUT
    assert result.database.latency_ms < 1000
    assert not result.ready


@pytest.mark.asyncio
async def test_unknown_schema_does_not_query_business_tables():
    repo = FakeDiagnosticsRepository()
    repo.revision_error = ProgrammingError("SELECT", {}, Exception("undefined table"))
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.status is Status.UNKNOWN
    assert result.components.migration.code is Code.MIGRATION_UNKNOWN
    assert repo.calls == ["ping", "revisions"]


@pytest.mark.asyncio
async def test_global_healthy_pilot_and_disabled_tasks_are_not_failures():
    result = await DiagnosticsService(FakeDiagnosticsRepository(), settings()).diagnostics(True)
    assert result.status is Status.OK
    assert result.format_version == 1
    assert result.version.schema_revision == EXPECTED_SCHEMA_REVISION
    assert result.components.cloud_tasks_inbound.code is Code.INBOUND_TASKS_DISABLED
    assert result.components.cloud_tasks_outbound.code is Code.OUTBOUND_TASKS_DISABLED
    assert result.components.whatsapp.details.legacy_pilot_present
    assert not result.components.whatsapp.details.remote_checked
    assert set(type(result.components).model_fields) == {
        "application", "database", "migration", "cloud_tasks_inbound",
        "cloud_tasks_outbound", "whatsapp", "credential_provider", "recent_activity",
    }


@pytest.mark.asyncio
async def test_tasks_are_separate_and_misconfigured_outbound_degrades():
    configured = settings().model_copy(update={
        "cloud_tasks_enabled": True, "outbound_tasks_enabled": True,
        "gcp_project_id": "test-project", "gcp_region": "test-region",
        "cloud_tasks_events_queue": "test-inbound",
        "cloud_tasks_target_url": "https://test.example.run.app/internal/tasks/whatsapp-event",
        "cloud_tasks_outbound_target_url": None,
    })
    report = await DiagnosticsService(FakeDiagnosticsRepository(), configured).diagnostics(True)
    assert report.status is Status.DEGRADED
    assert report.components.cloud_tasks_inbound.status is Status.OK
    assert report.components.cloud_tasks_outbound.code is Code.OUTBOUND_TASKS_MISCONFIGURED
    assert "test-project" not in report.model_dump_json()
    assert "test.example.run.app" not in report.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "code"), [
    ("pending", Code.META_CONNECTION_PENDING),
    ("current_disconnected", Code.META_CONNECTION_DISCONNECTED),
    ("error", Code.META_CONNECTION_ERROR),
    ("missing_phone", Code.META_PHONE_NOT_CONFIGURED),
])
async def test_one_tenant_issue_degrades_not_global_error(field, code):
    repo = FakeDiagnosticsRepository()
    repo.summary.update(observed=20, connected=19, **{field: 1})
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.status is Status.DEGRADED
    assert code in result.components.whatsapp.details.issues


@pytest.mark.asyncio
async def test_secret_references_do_not_imply_credentials_are_accessible():
    repo = FakeDiagnosticsRepository()
    repo.summary.update(observed=2, connected=2, reference_count=2, credential_required=2)
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.components.whatsapp.code is Code.META_CREDENTIAL_UNAVAILABLE
    assert result.components.credential_provider.code is Code.CREDENTIAL_PROVIDER_UNAVAILABLE
    assert result.components.whatsapp.details.credential_references_configured == 2


@pytest.mark.asyncio
async def test_optional_query_failure_is_unknown_not_false_healthy():
    repo = FakeDiagnosticsRepository()
    repo.summary_error = RuntimeError("raw payload")
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.status is Status.UNKNOWN
    assert result.components.whatsapp.status is Status.UNKNOWN
    assert "raw payload" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_missing_meta_does_not_prevent_readiness():
    config = settings().model_copy(update={
        "meta_access_token": None, "meta_phone_number_id": None,
        "meta_app_secret": None, "meta_verify_token": None,
    })
    service = DiagnosticsService(FakeDiagnosticsRepository(), config)
    assert (await service.readiness(True)).ready
    assert (await service.diagnostics(True)).status is Status.DEGRADED


def test_details_forbid_arbitrary_sensitive_fields():
    with pytest.raises(ValidationError):
        ComponentResult[DatabaseDetails](
            status=Status.OK, checked_at=datetime.now(UTC),
            details={"reachable": True, "password": "forbidden"},
        )
    assert Settings(_env_file=None, APP_COMMIT_SHA="private-value").app_commit_sha is None


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "", "Basic fake", "Bearer "])
async def test_anonymous_or_malformed_authorization_rejected(header):
    with pytest.raises(HTTPException) as caught:
        await require_diagnostics_oidc(settings(), header)
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_partial_diagnostics_identity_fails_closed():
    config = settings().model_copy(update={"diagnostics_oidc_audience": "https://dedicated.example"})
    with pytest.raises(HTTPException) as caught:
        await require_diagnostics_oidc(config, "Bearer fake")
    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_oidc_uses_dedicated_pair_while_task_flags_disabled(monkeypatch):
    config = settings().model_copy(update={
        "diagnostics_oidc_audience": "https://dedicated.example",
        "diagnostics_invoker_email": "reader@example.iam.gserviceaccount.com",
    })
    seen = []

    def verify(token, audience):
        seen.append((token, audience))
        return {"email": config.diagnostics_invoker_email, "email_verified": True}

    monkeypatch.setattr(auth, "_verify_google_oidc_token", verify)
    await require_diagnostics_oidc(config, "Bearer signed-test-token")
    assert seen == [("signed-test-token", "https://dedicated.example")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("claims", "expected"), [
    ({"email": "wrong@example.com", "email_verified": True}, 403),
    ({"email": "diagnostic@test.iam.gserviceaccount.com", "email_verified": False}, 403),
    (None, 401),
])
async def test_invalid_oidc_identity_or_signature_rejected(monkeypatch, claims, expected):
    def verify(*_):
        if claims is None:
            raise ValueError("token with secret")
        return claims
    monkeypatch.setattr(auth, "_verify_google_oidc_token", verify)
    with pytest.raises(HTTPException) as caught:
        await require_diagnostics_oidc(settings(), "Bearer secret")
    assert caught.value.status_code == expected
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_private_endpoint_auth_and_safe_machine_readable_response(client: AsyncClient, monkeypatch, caplog):
    from app.core.config import get_settings
    repo = FakeDiagnosticsRepository()
    service = DiagnosticsService(repo, settings())
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_diagnostics_service] = lambda: service
    monkeypatch.setattr(app.state, "initialized", True)
    monkeypatch.setattr(auth, "_verify_google_oidc_token", lambda *_: {
        "email": settings().cloud_tasks_invoker_email, "email_verified": True,
    })
    caplog.set_level(logging.INFO)
    try:
        unauthorized = await client.get("/internal/diagnostics")
        assert unauthorized.status_code == 401
        assert repo.calls == []
        response = await client.get("/internal/diagnostics", headers={"Authorization": "Bearer hidden-token"})
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_diagnostics_service, None)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["status"] == "ok"
    assert response.json()["format_version"] == 1
    for secret in ("hidden-token", "test-access-token", "test-phone-number-id", "test-waba-id"):
        assert secret not in response.text + caplog.text


@pytest.mark.asyncio
async def test_liveness_ignores_dependencies_and_readiness_checks_lifespan_schema(client, monkeypatch):
    repo = FakeDiagnosticsRepository()
    app.dependency_overrides[get_diagnostics_service] = lambda: DiagnosticsService(repo, settings())
    try:
        monkeypatch.setattr(app.state, "initialized", False)
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert repo.calls == []
        assert (await client.get("/ready")).status_code == 503
        async with app.router.lifespan_context(app):
            assert (await client.get("/ready")).status_code == 200
            repo.revision_values = ["20260902_0004"]
            result = await client.get("/ready")
            assert result.status_code == 503
            assert result.json() == {"status": "not_ready", "database": "connected"}
        assert app.state.initialized is False
    finally:
        app.dependency_overrides.pop(get_diagnostics_service, None)


@pytest.mark.asyncio
async def test_blank_dedicated_identity_keeps_existing_oidc_fallback(monkeypatch):
    config = Settings(_env_file=None, DIAGNOSTICS_OIDC_AUDIENCE="",
        DIAGNOSTICS_INVOKER_EMAIL=" ", CLOUD_TASKS_OIDC_AUDIENCE="https://reader.example",
        CLOUD_TASKS_INVOKER_EMAIL="reader@example.iam.gserviceaccount.com")
    seen = []

    def verify(_, audience):
        seen.append(audience)
        return {"email": config.cloud_tasks_invoker_email, "email_verified": True}

    monkeypatch.setattr(auth, "_verify_google_oidc_token", verify)
    await require_diagnostics_oidc(config, "Bearer test-only")
    assert seen == ["https://reader.example"]


@pytest.mark.asyncio
async def test_historical_disconnected_connection_does_not_degrade_healthy_replacement():
    repo = FakeDiagnosticsRepository()
    repo.summary.update(observed=2, connected=1, disconnected=1, current_disconnected=0)
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.status is Status.OK
    assert result.components.whatsapp.details.connections.disconnected == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated", [False, True])
async def test_recent_failures_and_truncation_are_explicit(monkeypatch, truncated):
    from app.diagnostics.models import ActivityDetails

    async def activity(since):
        return ActivityDetails(sampled_since=since, failed_outbound_count=1, truncated=truncated)

    repo = FakeDiagnosticsRepository()
    monkeypatch.setattr(repo, "activity", activity)
    report = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert report.status is Status.DEGRADED
    assert report.components.recent_activity.code is (
        Code.OBSERVATION_TRUNCATED if truncated else Code.RECENT_OUTBOUND_FAILURES
    )


@pytest.mark.asyncio
async def test_schema_read_error_never_runs_business_queries():
    repo = FakeDiagnosticsRepository()
    repo.revision_error = ProgrammingError("private SQL", {}, Exception("private-exception-value"))
    result = await DiagnosticsService(repo, settings()).diagnostics(True)
    assert result.components.migration.code is Code.MIGRATION_UNKNOWN
    assert result.status is Status.UNKNOWN
    assert repo.calls == ["ping", "revisions"]
    assert "private-exception-value" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("initialized", "revision", "code"), [
    (False, EXPECTED_SCHEMA_REVISION, Code.APPLICATION_NOT_READY),
    (True, "20260902_0004", Code.MIGRATION_BEHIND),
    (True, "20260903_0006", Code.MIGRATION_AHEAD),
])
async def test_known_essential_failure_is_global_error(initialized, revision, code):
    repo = FakeDiagnosticsRepository()
    repo.revision_values = [revision]
    result = await DiagnosticsService(repo, settings()).diagnostics(initialized)
    assert result.status is Status.ERROR
    assert code in {result.components.application.code, result.components.migration.code}
    assert repo.calls == ["ping", "revisions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sha", [None, "b" * 40, "malformed-not-a-commit"])
async def test_commit_sha_optional_and_sanitized(sha, client, monkeypatch):
    monkeypatch.delenv("APP_COMMIT_SHA", raising=False)
    version = Settings(_env_file=None, **({"APP_COMMIT_SHA": sha} if sha is not None else {})).app_commit_sha
    config = settings().model_copy(update={"app_commit_sha": version})
    repo = FakeDiagnosticsRepository()
    service = DiagnosticsService(repo, config)
    result = await service.diagnostics(True)
    assert result.version.commit == (sha if sha == "b" * 40 else None)
    assert result.status is Status.OK
    app.dependency_overrides[get_diagnostics_service] = lambda: service
    monkeypatch.setattr(app.state, "initialized", True)
    try:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200
    finally:
        app.dependency_overrides.pop(get_diagnostics_service, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_identity", [False, True])
async def test_arbitrary_headers_cannot_authenticate_production(client, monkeypatch, configured_identity):
    config = settings()
    if not configured_identity:
        config = config.model_copy(update={"cloud_tasks_oidc_audience": None, "cloud_tasks_invoker_email": None})
    repo = FakeDiagnosticsRepository()
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_diagnostics_service] = lambda: DiagnosticsService(repo, config)

    def unexpected_verification(*_):
        raise AssertionError("Header-only request must never reach token verification")

    monkeypatch.setattr(auth, "_verify_google_oidc_token", unexpected_verification)
    try:
        response = await client.get("/internal/diagnostics", headers={
            "X-CloudTasks-TaskName": "forged-task",
            "X-Goog-Authenticated-User-Email": "accounts.google.com:diagnostic@test.iam.gserviceaccount.com",
            "X-Serverless-Authorization": "Bearer forged-token",
            "X-Environment": "development",
        })
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_diagnostics_service, None)
    assert response.status_code == 401
    assert repo.calls == []
    assert "forged" not in response.text


@pytest.mark.asyncio
async def test_health_performs_no_dependency_io_even_when_every_dependency_fails(client, monkeypatch):
    import socket
    from app.core import database

    def forbidden(*_, **__):
        pytest.fail("Liveness must not access a dependency")

    monkeypatch.setattr(database, "get_engine", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(auth, "_verify_google_oidc_token", forbidden)
    app.dependency_overrides[get_diagnostics_service] = forbidden
    try:
        response = await client.get("/health")
    finally:
        app.dependency_overrides.pop(get_diagnostics_service, None)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_all_component_configuration_checks_are_local(monkeypatch):
    import socket

    def forbidden(*_, **__):
        pytest.fail("Configuration diagnostics must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    config = settings().model_copy(update={
        "cloud_tasks_enabled": True, "outbound_tasks_enabled": True,
        "gcp_project_id": "test-project", "gcp_region": "test-region",
        "cloud_tasks_events_queue": "test-inbound",
        "cloud_tasks_target_url": "https://test.example.run.app/internal/tasks/whatsapp-event",
        "cloud_tasks_outbound_target_url": "https://test.example.run.app/internal/tasks/whatsapp-outbound",
    })
    report = await DiagnosticsService(FakeDiagnosticsRepository(), config).diagnostics(True)
    assert report.status is Status.OK
    assert report.components.cloud_tasks_inbound.details.configured
    assert report.components.cloud_tasks_outbound.details.configured

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy.exc import DBAPIError, SQLAlchemyError, TimeoutError as PoolTimeout

from app.core.config import DatabaseConfigurationError, Settings
from app.diagnostics.models import (
    ACTIVITY_WINDOW_HOURS, EXPECTED_SCHEMA_REVISION, OBSERVATION_LIMIT,
    QUERY_TIMEOUT_SECONDS, SCHEMA_REVISIONS, ActivityDetails, ApplicationDetails,
    AutomationStatus, BusinessDiagnostics, ComponentResult, Components,
    ConnectionCounts, CredentialDetails, DatabaseDetails, DiagnosticCode as Code,
    DiagnosticStatus as Status, DiagnosticsResponse, EssentialComponents,
    MigrationDetails, TasksDetails, VersionDetails, WhatsAppDetails,
)
from app.diagnostics.repository import DiagnosticsRepository
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus

T = TypeVar("T")


def now() -> datetime:
    return datetime.now(UTC)


def database_error_code(exc: Exception) -> Code:
    chain = (exc, getattr(exc, "orig", None), exc.__cause__)
    states = {getattr(value, "sqlstate", None) for value in chain}
    if isinstance(exc, (TimeoutError, PoolTimeout)) or "57014" in states:
        return Code.DB_TIMEOUT
    if (
        isinstance(exc, (OSError, DatabaseConfigurationError))
        or any(isinstance(state, str) and state.startswith(("08", "28")) for state in states)
        or (isinstance(exc, DBAPIError) and exc.connection_invalidated)
    ):
        return Code.DB_UNREACHABLE
    if isinstance(exc, SQLAlchemyError):
        return Code.DB_QUERY_FAILED
    return Code.UNKNOWN_COMPONENT_ERROR


async def observe(operation: Callable[[], Awaitable[T]]) -> tuple[T | None, Code | None, float]:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            value = await operation()
        code = None
    except Exception as exc:
        # Do not serialize/log exceptions: they may contain SQL parameters or DSNs.
        value, code = None, database_error_code(exc)
    return value, code, round((time.perf_counter() - started) * 1000, 2)


def migration_result(revisions: list[str] | None, latency: float | None) -> ComponentResult[MigrationDetails]:
    revision = revisions[0] if revisions is not None and len(revisions) == 1 else None
    # Unknown strings are never echoed. Only migration identifiers are safe here.
    safe_revision = revision if revision and re.fullmatch(r"\d{8}_\d{4}", revision) else None
    code, state = Code.MIGRATION_UNKNOWN, Status.UNKNOWN
    if revision == EXPECTED_SCHEMA_REVISION:
        code, state = Code.MIGRATION_OK, Status.OK
    elif revision in SCHEMA_REVISIONS:
        code, state = Code.MIGRATION_BEHIND, Status.ERROR
    elif safe_revision and safe_revision > EXPECTED_SCHEMA_REVISION:
        # A later dated/sequenced identifier is ahead, never assumed compatible.
        code, state = Code.MIGRATION_AHEAD, Status.ERROR
    return ComponentResult(
        status=state, code=code, latency_ms=latency, checked_at=now(),
        details=MigrationDetails(current_revision=safe_revision),
    )


def overall_status(components: Components) -> Status:
    essential = [components.application, components.database, components.migration]
    if any(c.status is Status.ERROR for c in essential):
        return Status.ERROR
    if any(c.status is not Status.OK for c in essential):
        return Status.UNKNOWN
    optional = [components.cloud_tasks_inbound, components.cloud_tasks_outbound,
                components.whatsapp, components.credential_provider, components.recent_activity]
    # Async dispatch can be degraded while sync inbound / durable outbox still work.
    # Likewise one tenant's broken connection does not make the database unusable.
    if any(c.status in {Status.ERROR, Status.DEGRADED} for c in optional):
        return Status.DEGRADED
    if any(c.status is Status.UNKNOWN for c in optional):
        return Status.UNKNOWN
    return Status.OK


class DiagnosticsService:
    def __init__(self, repository: DiagnosticsRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def readiness(self, initialized: bool) -> EssentialComponents:
        application = ComponentResult(
            status=Status.OK if initialized else Status.ERROR,
            code=None if initialized else Code.APPLICATION_NOT_READY,
            checked_at=now(), latency_ms=0,
            details=ApplicationDetails(initialized=initialized),
        )
        _, error, latency = await observe(self.repository.ping)
        database = ComponentResult(
            status=Status.OK if error is None else Status.ERROR,
            code=error, checked_at=now(), latency_ms=latency,
            details=DatabaseDetails(reachable=error is None),
        )
        revision_values, migration_latency = None, None
        if error is None:
            revision_values, _, migration_latency = await observe(self.repository.revisions)
        return EssentialComponents(
            application=application, database=database,
            migration=migration_result(revision_values, migration_latency),
        )

    def tasks(self, *, outbound: bool) -> ComponentResult[TasksDetails]:
        enabled = self.settings.outbound_tasks_enabled if outbound else self.settings.cloud_tasks_enabled
        configured = False
        try:
            # Check even disabled config, without changing runtime flags or clients.
            checked_settings = self.settings.model_copy(update={
                "outbound_tasks_enabled" if outbound else "cloud_tasks_enabled": True,
            })
            if outbound:
                checked_settings.require_outbound_tasks_configuration()
            else:
                checked_settings.require_cloud_tasks_configuration()
            configured = True
        except Exception:
            pass
        disabled_code = Code.OUTBOUND_TASKS_DISABLED if outbound else Code.INBOUND_TASKS_DISABLED
        invalid_code = Code.OUTBOUND_TASKS_MISCONFIGURED if outbound else Code.INBOUND_TASKS_MISCONFIGURED
        return ComponentResult(
            status=Status.DEGRADED if enabled and not configured else Status.OK,
            code=(invalid_code if not configured else None) if enabled else disabled_code,
            checked_at=now(), latency_ms=0,
            details=TasksDetails(enabled=enabled, configured=configured),
        )

    def _credential_configured(self) -> bool:
        token = self.settings.meta_access_token
        raw = token.get_secret_value() if token else ""
        return bool(raw and raw == raw.strip())

    def _graph_configured(self) -> bool:
        return bool(re.fullmatch(r"v\d{1,3}\.\d{1,3}", self.settings.meta_graph_version or ""))

    def _webhook_configured(self) -> bool:
        try:
            self.settings.require_meta_app_secret()
            self.settings.require_meta_verify_token()
            return True
        except Exception:
            return False

    def _whatsapp_details(self, row: dict[str, Any]) -> WhatsAppDetails:
        unavailable = row["credential_required"] - (
            row["legacy_credential_eligible"] if self._credential_configured() else 0
        )
        legacy = bool(row["legacy_pilot"])
        if legacy and not self._credential_configured():
            unavailable += 1
        missing_graph = 0 if self._graph_configured() else row["missing_graph"] + int(legacy)
        issues: list[Code] = []
        if (not row["observed"] and not legacy) or not self._webhook_configured() or missing_graph:
            issues.append(Code.META_NOT_CONFIGURED)
        for field, code in (
            ("pending", Code.META_CONNECTION_PENDING),
            ("current_disconnected", Code.META_CONNECTION_DISCONNECTED),
            ("error", Code.META_CONNECTION_ERROR),
            ("missing_phone", Code.META_PHONE_NOT_CONFIGURED),
        ):
            if row[field]:
                issues.append(code)
        if unavailable:
            issues.append(Code.META_CREDENTIAL_UNAVAILABLE)
        if row["observed"] > OBSERVATION_LIMIT:
            issues.append(Code.OBSERVATION_TRUNCATED)
        return WhatsAppDetails(
            connections=ConnectionCounts(**{k: row[k] for k in ("connected", "pending", "disconnected", "error")}),
            legacy_pilot_present=legacy, credential_references_configured=row["reference_count"],
            unavailable_credential_count=unavailable, missing_phone_count=row["missing_phone"],
            graph_not_configured_count=missing_graph, webhook_configured=self._webhook_configured(),
            current_disconnected_count=row["current_disconnected"],
            truncated=row["observed"] > OBSERVATION_LIMIT, issues=issues,
        )

    async def diagnostics(self, initialized: bool) -> DiagnosticsResponse:
        essential = await self.readiness(initialized)
        since = now() - timedelta(hours=ACTIVITY_WINDOW_HOURS)
        summary, activity, summary_error, activity_error = None, None, None, None
        summary_ms, activity_ms = None, None
        # Never query application tables against unknown/incompatible schemas.
        if essential.ready:
            (summary, summary_error, summary_ms), (activity, activity_error, activity_ms) = await asyncio.gather(
                observe(lambda: self.repository.whatsapp_summary(self.settings.meta_phone_number_id)),
                observe(lambda: self.repository.activity(since)),
            )
        details = self._whatsapp_details(summary) if summary is not None else WhatsAppDetails()
        whatsapp = ComponentResult(
            status=Status.UNKNOWN if summary is None else (Status.DEGRADED if details.issues else Status.OK),
            code=(summary_error or Code.UNKNOWN_COMPONENT_ERROR) if summary is None else (details.issues[0] if details.issues else None),
            latency_ms=summary_ms, checked_at=now(), details=details,
        )
        unavailable = details.unavailable_credential_count if summary is not None else None
        credentials = ComponentResult(
            status=Status.UNKNOWN if unavailable is None else (Status.DEGRADED if unavailable else Status.OK),
            code=Code.UNKNOWN_COMPONENT_ERROR if unavailable is None else (Code.CREDENTIAL_PROVIDER_UNAVAILABLE if unavailable else None),
            latency_ms=0, checked_at=now(), details=CredentialDetails(
                legacy_configured=self._credential_configured(), unavailable_connections=unavailable,
            ),
        )
        activity_code = None
        if activity is not None:
            if activity.truncated:
                activity_code = Code.OBSERVATION_TRUNCATED
            elif activity.failed_outbound_count:
                activity_code = Code.RECENT_OUTBOUND_FAILURES
        recent = ComponentResult(
            status=Status.UNKNOWN if activity is None else (Status.DEGRADED if activity_code else Status.OK),
            code=(activity_error or Code.UNKNOWN_COMPONENT_ERROR) if activity is None else activity_code,
            latency_ms=activity_ms, checked_at=now(), details=activity or ActivityDetails(sampled_since=since),
        )
        components = Components(
            **{field: getattr(essential, field) for field in ("application", "database", "migration")},
            cloud_tasks_inbound=self.tasks(outbound=False), cloud_tasks_outbound=self.tasks(outbound=True),
            whatsapp=whatsapp, credential_provider=credentials, recent_activity=recent,
        )
        return DiagnosticsResponse(
            status=overall_status(components), timestamp=now(), environment=self.settings.environment,
            version=VersionDetails(commit=self.settings.app_commit_sha,
                schema_revision=essential.migration.details.current_revision),
            components=components,
        )

    async def business_diagnostics(self, business_id: UUID) -> BusinessDiagnostics | None:
        """Internal service only: caller must authorize the tenant before invocation."""
        checked_at = now()
        since = checked_at - timedelta(hours=ACTIVITY_WINDOW_HOURS)
        essential = await self.readiness(True)
        activity = ActivityDetails(sampled_since=since)
        if not essential.ready:
            return BusinessDiagnostics(
                business_id=business_id, status=Status.UNKNOWN,
                code=Code.UNKNOWN_COMPONENT_ERROR, checked_at=checked_at, activity=activity,
            )
        row, error, _ = await observe(lambda: self.repository.business_snapshot(
            business_id, self.settings.meta_phone_number_id, checked_at,
        ))
        if error:
            return BusinessDiagnostics(business_id=business_id, status=Status.UNKNOWN,
                code=error, checked_at=checked_at, activity=activity)
        if row is None:
            return None
        measured, activity_error, _ = await observe(lambda: self.repository.activity(since, business_id))
        activity = measured or activity
        connection_truncated = row["connections_observed"] > OBSERVATION_LIMIT
        code = Code.OBSERVATION_TRUNCATED if connection_truncated else None
        status_codes = {
            WhatsAppConnectionStatus.PENDING: Code.META_CONNECTION_PENDING,
            WhatsAppConnectionStatus.DISCONNECTED: Code.META_CONNECTION_DISCONNECTED,
            WhatsAppConnectionStatus.ERROR: Code.META_CONNECTION_ERROR,
        }
        connection_status = (
            WhatsAppConnectionStatus(row["status"])
            if row["status"] and not connection_truncated else None
        )
        legacy = bool(row["legacy_pilot"])
        if connection_truncated:
            pass  # A capped history cannot identify the current connection safely.
        elif row["has_connection"]:
            code = status_codes.get(connection_status)
            if code is None and not row["has_phone"]:
                code = Code.META_PHONE_NOT_CONFIGURED
            if code is None and (row["has_reference"] or not row["matches_legacy"] or not self._credential_configured()):
                code = Code.META_CREDENTIAL_UNAVAILABLE
            if code is None and not row["has_graph"] and not self._graph_configured():
                code = Code.META_NOT_CONFIGURED
        elif not legacy:
            code = Code.META_NOT_CONFIGURED
        elif not self._credential_configured():
            code = Code.META_CREDENTIAL_UNAVAILABLE
        elif not self._graph_configured():
            code = Code.META_NOT_CONFIGURED
        if not self._webhook_configured():
            code = code or Code.META_NOT_CONFIGURED
        automation = AutomationStatus.ENABLED
        if not row["active"]:
            automation = AutomationStatus.DISABLED
        elif row["observed"] > OBSERVATION_LIMIT or row["exclusions_observed"] > OBSERVATION_LIMIT:
            automation = AutomationStatus.UNKNOWN
        elif row["observed"] and row["blocked"] == row["observed"]:
            automation = AutomationStatus.HUMAN_CONTROLLED
        elif row["blocked"] or row["has_exclusions"]:
            automation = AutomationStatus.MIXED
        truncated = (
            row["observed"] > OBSERVATION_LIMIT or row["exclusions_observed"] > OBSERVATION_LIMIT
            or connection_truncated or activity.truncated
        )
        code = code or (Code.RECENT_OUTBOUND_FAILURES if activity.failed_outbound_count else None)
        status = (
            Status.UNKNOWN if connection_truncated
            else Status.DEGRADED if code else Status.UNKNOWN if activity_error else Status.OK
        )
        code = code or activity_error or (Code.OBSERVATION_TRUNCATED if truncated else None)
        if status is Status.OK and truncated:
            status = Status.DEGRADED
        return BusinessDiagnostics(
            business_id=business_id, status=status, code=code,
            connection_mode=WhatsAppConnectionMode(row["mode"]) if row["mode"] and not connection_truncated else None,
            connection_status=connection_status, legacy_pilot=legacy, automation_status=automation,
            last_inbound_at=activity.last_inbound_at, last_outbound_at=activity.last_outbound_at,
            last_successful_outbound_at=activity.last_successful_outbound_at,
            pending_outbound_count=activity.pending_outbound_count, failed_outbound_count=activity.failed_outbound_count,
            # Do not echo arbitrary last_error_code strings, even if DB regex accepts them.
            last_sanitized_error_code=Code.META_CONNECTION_ERROR if row["has_error"] and not connection_truncated else None,
            checked_at=checked_at, activity=activity,
        )

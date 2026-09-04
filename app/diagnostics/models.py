from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Environment
from app.whatsapp.connections import WhatsAppConnectionMode, WhatsAppConnectionStatus

EXPECTED_SCHEMA_REVISION = "20260904_0007"
SCHEMA_REVISIONS = (
    "20260901_0001", "20260901_0002", "20260901_0003",
    "20260902_0004", "20260902_0005", "20260903_0006", EXPECTED_SCHEMA_REVISION,
)
QUERY_TIMEOUT_SECONDS = 2.0
ACTIVITY_WINDOW_HOURS = 24
OBSERVATION_LIMIT = 10_000


class DiagnosticStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


class DiagnosticCode(StrEnum):
    APPLICATION_NOT_READY = "APPLICATION_NOT_READY"
    DB_UNREACHABLE = "DB_UNREACHABLE"
    DB_TIMEOUT = "DB_TIMEOUT"
    DB_QUERY_FAILED = "DB_QUERY_FAILED"
    MIGRATION_UNKNOWN = "MIGRATION_UNKNOWN"
    MIGRATION_BEHIND = "MIGRATION_BEHIND"
    MIGRATION_AHEAD = "MIGRATION_AHEAD"
    MIGRATION_OK = "MIGRATION_OK"
    INBOUND_TASKS_DISABLED = "INBOUND_TASKS_DISABLED"
    INBOUND_TASKS_MISCONFIGURED = "INBOUND_TASKS_MISCONFIGURED"
    OUTBOUND_TASKS_DISABLED = "OUTBOUND_TASKS_DISABLED"
    OUTBOUND_TASKS_MISCONFIGURED = "OUTBOUND_TASKS_MISCONFIGURED"
    META_NOT_CONFIGURED = "META_NOT_CONFIGURED"
    META_CONNECTION_PENDING = "META_CONNECTION_PENDING"
    META_CONNECTION_DISCONNECTED = "META_CONNECTION_DISCONNECTED"
    META_CONNECTION_ERROR = "META_CONNECTION_ERROR"
    META_CREDENTIAL_UNAVAILABLE = "META_CREDENTIAL_UNAVAILABLE"
    META_PHONE_NOT_CONFIGURED = "META_PHONE_NOT_CONFIGURED"
    CREDENTIAL_PROVIDER_UNAVAILABLE = "CREDENTIAL_PROVIDER_UNAVAILABLE"
    UNKNOWN_COMPONENT_ERROR = "UNKNOWN_COMPONENT_ERROR"
    OBSERVATION_TRUNCATED = "OBSERVATION_TRUNCATED"
    RECENT_OUTBOUND_FAILURES = "RECENT_OUTBOUND_FAILURES"


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplicationDetails(SafeModel):
    initialized: bool


class DatabaseDetails(SafeModel):
    reachable: bool


class MigrationDetails(SafeModel):
    expected_revision: str = EXPECTED_SCHEMA_REVISION
    current_revision: str | None = None


class TasksDetails(SafeModel):
    enabled: bool
    configured: bool
    # Configuration validation only, not evidence that the physical queue exists.
    remote_checked: bool = False


class ConnectionCounts(SafeModel):
    connected: int = 0
    pending: int = 0
    disconnected: int = 0
    error: int = 0


class WhatsAppDetails(SafeModel):
    connections: ConnectionCounts = Field(default_factory=ConnectionCounts)
    legacy_pilot_present: bool = False
    credential_references_configured: int = 0
    missing_phone_count: int = 0
    unavailable_credential_count: int = 0
    graph_not_configured_count: int = 0
    webhook_configured: bool = False
    current_disconnected_count: int = 0
    truncated: bool = False
    remote_checked: bool = False
    issues: list[DiagnosticCode] = Field(default_factory=list)


class CredentialDetails(SafeModel):
    legacy_configured: bool
    secret_reference_supported: bool = False
    unavailable_connections: int | None = None
    remote_checked: bool = False


class ActivityDetails(SafeModel):
    window_hours: int = ACTIVITY_WINDOW_HOURS
    sampled_since: datetime
    sample_limit: int = OBSERVATION_LIMIT
    selection: Literal["recent_global", "bounded_business"] = "recent_global"
    truncated: bool = False
    last_webhook_processed_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    last_successful_outbound_at: datetime | None = None
    pending_outbound_count: int = 0
    pending_scope: Literal["all_ages"] = "all_ages"
    failed_outbound_count: int = 0
    # No sent_at exists: updated_at is a status-update proxy, NOT Meta send time.
    success_time_source: Literal["last_status_update"] = "last_status_update"


D = TypeVar("D", bound=SafeModel)


class ComponentResult(SafeModel, Generic[D]):
    status: DiagnosticStatus
    code: DiagnosticCode | None = None
    latency_ms: float | None = None
    checked_at: datetime
    details: D


class EssentialComponents(SafeModel):
    application: ComponentResult[ApplicationDetails]
    database: ComponentResult[DatabaseDetails]
    migration: ComponentResult[MigrationDetails]

    @property
    def ready(self) -> bool:
        return all(c.status is DiagnosticStatus.OK for c in (
            self.application, self.database, self.migration,
        ))


class Components(EssentialComponents):
    cloud_tasks_inbound: ComponentResult[TasksDetails]
    cloud_tasks_outbound: ComponentResult[TasksDetails]
    whatsapp: ComponentResult[WhatsAppDetails]
    credential_provider: ComponentResult[CredentialDetails]
    recent_activity: ComponentResult[ActivityDetails]


class VersionDetails(SafeModel):
    commit: str | None
    schema_revision: str | None


class DiagnosticsResponse(SafeModel):
    format_version: Literal[1] = 1
    status: DiagnosticStatus
    timestamp: datetime
    service: Literal["whatsapp-backend"] = "whatsapp-backend"
    environment: Environment
    version: VersionDetails
    components: Components


class AutomationStatus(StrEnum):
    ENABLED = "enabled"
    MIXED = "mixed"
    HUMAN_CONTROLLED = "human_controlled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class BusinessDiagnostics(SafeModel):
    business_id: UUID
    status: DiagnosticStatus
    code: DiagnosticCode | None = None
    connection_mode: WhatsAppConnectionMode | None = None
    connection_status: WhatsAppConnectionStatus | None = None
    legacy_pilot: bool = False
    automation_status: AutomationStatus = AutomationStatus.UNKNOWN
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    last_successful_outbound_at: datetime | None = None
    pending_outbound_count: int = 0
    failed_outbound_count: int = 0
    last_sanitized_error_code: DiagnosticCode | None = None
    checked_at: datetime
    activity: ActivityDetails

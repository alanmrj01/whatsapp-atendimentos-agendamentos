from datetime import datetime
from uuid import UUID

from app.diagnostics.models import ActivityDetails, EXPECTED_SCHEMA_REVISION


class FakeDiagnosticsRepository:
    def __init__(self) -> None:
        self.revision_values = [EXPECTED_SCHEMA_REVISION]
        self.ping_error: Exception | None = None
        self.revision_error: Exception | None = None
        self.summary_error: Exception | None = None
        self.summary = dict(
            observed=0, connected=0, pending=0, disconnected=0, error=0,
            current_disconnected=0,
            reference_count=0, missing_phone=0, credential_required=0,
            legacy_credential_eligible=0, missing_graph=0, legacy_pilot=True,
        )
        self.calls: list[str] = []

    async def ping(self) -> None:
        self.calls.append("ping")
        if self.ping_error:
            raise self.ping_error

    async def revisions(self) -> list[str]:
        self.calls.append("revisions")
        if self.revision_error:
            raise self.revision_error
        return self.revision_values

    async def whatsapp_summary(self, _: str | None):
        self.calls.append("summary")
        if self.summary_error:
            raise self.summary_error
        return self.summary

    async def activity(self, since: datetime, business_id: UUID | None = None):
        self.calls.append("activity")
        return ActivityDetails(sampled_since=since)


async def essential_report(*, connected: bool = True):
    from app.core.config import Settings
    from app.diagnostics.service import DiagnosticsService

    repo = FakeDiagnosticsRepository()
    if not connected:
        repo.ping_error = OSError("unavailable")
    return await DiagnosticsService(repo, Settings(_env_file=None)).readiness(True)

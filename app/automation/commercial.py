from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from app.repositories.commercial_automation import (
    CommercialAutomationRepository,
)
from app.schemas.commercial_automation import CleaningReminderDashboard


class CommercialAutomationService:
    def __init__(
        self,
        repository: CommercialAutomationRepository,
        *,
        now: Callable[[], datetime] | None = None,
        followup_24h_enabled: bool = False,
        cleaning_6m_enabled: bool = False,
    ) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.followup_24h_enabled = followup_24h_enabled
        self.cleaning_6m_enabled = cleaning_6m_enabled

    async def sweep(self) -> list[UUID]:
        current = self.now()
        enabled_kinds: set[str] = set()

        if self.followup_24h_enabled:
            enabled_kinds.add("abandoned_followup_24h")
            abandoned = await self.repository.abandoned_candidates(current)
            for candidate in abandoned:
                await self.repository.create_abandoned_followup(
                    candidate,
                    now=current,
                )

        if self.cleaning_6m_enabled:
            enabled_kinds.add("cleaning_reminder_6m")
            cleaning = await self.repository.cleaning_candidates(current)
            for candidate in cleaning:
                await self.repository.create_cleaning_reminder(
                    candidate,
                    now=current,
                )

        if not enabled_kinds:
            return []
        return await self.repository.pending_dispatch_ids(
            automation_kinds=enabled_kinds,
        )

    async def cleaning_dashboard(
        self,
        business_id: UUID,
    ) -> CleaningReminderDashboard:
        return await self.repository.dashboard(
            business_id,
            self.now(),
        )

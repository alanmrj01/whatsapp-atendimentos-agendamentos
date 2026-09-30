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
    ) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def sweep(self) -> list[UUID]:
        current = self.now()
        abandoned = await self.repository.abandoned_candidates(current)
        for candidate in abandoned:
            await self.repository.create_abandoned_followup(
                candidate,
                now=current,
            )

        cleaning = await self.repository.cleaning_candidates(current)
        for candidate in cleaning:
            await self.repository.create_cleaning_reminder(
                candidate,
                now=current,
            )

        return await self.repository.pending_dispatch_ids()

    async def cleaning_dashboard(
        self,
        business_id: UUID,
    ) -> CleaningReminderDashboard:
        return await self.repository.dashboard(
            business_id,
            self.now(),
        )

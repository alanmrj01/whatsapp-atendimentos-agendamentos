from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.automation.commercial import CommercialAutomationService
from app.repositories.commercial_automation import add_months


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
MESSAGE_ID = uuid.UUID("71000000-0000-0000-0000-000000000007")


class FakeCommercialRepository:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.abandoned = [SimpleNamespace(id="abandoned")]
        self.cleaning = [SimpleNamespace(id="cleaning")]

    async def abandoned_candidates(self, now: datetime):
        self.calls.append(("abandoned_candidates", now))
        return self.abandoned

    async def create_abandoned_followup(self, candidate, *, now: datetime):
        self.calls.append(("create_abandoned", candidate, now))
        return None

    async def cleaning_candidates(self, now: datetime):
        self.calls.append(("cleaning_candidates", now))
        return self.cleaning

    async def create_cleaning_reminder(self, candidate, *, now: datetime):
        self.calls.append(("create_cleaning", candidate, now))
        return None

    async def pending_dispatch_ids(self, *, automation_kinds: set[str]):
        self.calls.append(("pending", frozenset(automation_kinds)))
        return [MESSAGE_ID]


def test_add_months_preserves_calendar_semantics_at_month_end() -> None:
    source = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)

    assert add_months(source, 6) == datetime(
        2027, 2, 28, 15, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_commercial_sweep_is_dormant_by_default() -> None:
    repository = FakeCommercialRepository()
    service = CommercialAutomationService(
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    assert await service.sweep() == []
    assert repository.calls == []


@pytest.mark.asyncio
async def test_commercial_sweep_can_enable_only_24h_followup() -> None:
    repository = FakeCommercialRepository()
    service = CommercialAutomationService(
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
        followup_24h_enabled=True,
    )

    assert await service.sweep() == [MESSAGE_ID]
    assert [call[0] for call in repository.calls if isinstance(call, tuple)] == [
        "abandoned_candidates",
        "create_abandoned",
        "pending",
    ]
    assert repository.calls[-1] == (
        "pending",
        frozenset({"abandoned_followup_24h"}),
    )


@pytest.mark.asyncio
async def test_commercial_sweep_can_enable_only_cleaning_reminder() -> None:
    repository = FakeCommercialRepository()
    service = CommercialAutomationService(
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
        cleaning_6m_enabled=True,
    )

    assert await service.sweep() == [MESSAGE_ID]
    assert [call[0] for call in repository.calls if isinstance(call, tuple)] == [
        "cleaning_candidates",
        "create_cleaning",
        "pending",
    ]
    assert repository.calls[-1] == (
        "pending",
        frozenset({"cleaning_reminder_6m"}),
    )


@pytest.mark.asyncio
async def test_commercial_sweep_dispatches_both_kinds_when_explicitly_enabled() -> None:
    repository = FakeCommercialRepository()
    service = CommercialAutomationService(
        repository,  # type: ignore[arg-type]
        now=lambda: NOW,
        followup_24h_enabled=True,
        cleaning_6m_enabled=True,
    )

    assert await service.sweep() == [MESSAGE_ID]
    assert repository.calls[-1] == (
        "pending",
        frozenset(
            {
                "abandoned_followup_24h",
                "cleaning_reminder_6m",
            }
        ),
    )

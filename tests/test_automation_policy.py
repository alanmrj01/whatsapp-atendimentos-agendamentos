from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.automation.domain import (
    HUMAN_CONTROL_WINDOW_PRESETS,
    AutomationDecision,
    ExclusionMode,
    normalize_whatsapp_id,
)
from app.automation.service import (
    AutomationAdministrationService,
    AutomationPolicyService,
)
from app.models import BusinessAutomationExclusion
from app.repositories.automation import ConversationAutomationControl
from app.schemas.automation import (
    AutomationExclusionCreate,
    AutomationExclusionUpdate,
    BusinessAutomationSettings,
    BusinessAutomationSettingsUpdate,
)

BUSINESS_A = uuid.UUID("10000000-0000-0000-0000-000000000001")
BUSINESS_B = uuid.UUID("10000000-0000-0000-0000-000000000002")
CONVERSATION = uuid.UUID("30000000-0000-0000-0000-000000000001")
WHATSAPP_ID = "5511999990001"
NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)


class FakeAutomationRepository:
    def __init__(self) -> None:
        self.exclusions: dict[tuple[uuid.UUID, str], ExclusionMode] = {}
        self.controls: dict[uuid.UUID, ConversationAutomationControl] = {
            CONVERSATION: ConversationAutomationControl(None, "none")
        }
        self.window_minutes = 2160
        self.human_only_marked: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.human_only_cleared: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.suppressions_cleared: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.manual_events: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []

    async def get_active_exclusion_mode(
        self, business_id: uuid.UUID, whatsapp_id: str
    ) -> ExclusionMode | None:
        return self.exclusions.get((business_id, whatsapp_id))

    async def lock_conversation_control(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationAutomationControl:
        return self.controls[conversation_id]

    async def mark_human_only(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        self.human_only_marked.append((business_id, conversation_id))
        self.controls[conversation_id] = ConversationAutomationControl(
            self.controls[conversation_id].automation_suppressed_until,
            "human_only",
        )

    async def clear_human_only_marker(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        self.human_only_cleared.append((business_id, conversation_id))
        control = self.controls[conversation_id]
        if control.handoff_status == "human_only":
            self.controls[conversation_id] = ConversationAutomationControl(
                control.automation_suppressed_until,
                "none",
            )

    async def clear_temporary_suppression(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        self.suppressions_cleared.append((business_id, conversation_id))
        self.controls[conversation_id] = ConversationAutomationControl(
            None,
            self.controls[conversation_id].handoff_status,
        )

    async def activate_human_control(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> datetime:
        self.manual_events.append((business_id, conversation_id, occurred_at))
        until = occurred_at + timedelta(minutes=self.window_minutes)
        self.controls[conversation_id] = ConversationAutomationControl(
            until,
            self.controls[conversation_id].handoff_status,
        )
        return until


def test_human_control_defaults_to_36_hours() -> None:
    settings = BusinessAutomationSettings(business_id=BUSINESS_A)

    assert settings.human_control_window_minutes == 2160


@pytest.mark.parametrize("minutes", HUMAN_CONTROL_WINDOW_PRESETS)
def test_backend_accepts_only_closed_human_control_presets(minutes: int) -> None:
    settings = BusinessAutomationSettingsUpdate(
        human_control_window_minutes=minutes
    )

    assert settings.human_control_window_minutes == minutes


@pytest.mark.parametrize("minutes", [0, 4, 31, 2161, -5, True])
def test_backend_rejects_free_form_human_control_windows(minutes: int) -> None:
    with pytest.raises(ValidationError):
        BusinessAutomationSettingsUpdate(
            human_control_window_minutes=minutes
        )


def test_exclusion_schema_normalizes_individual_and_rejects_collective() -> None:
    values = AutomationExclusionCreate(
        whatsapp_id="+5511999990001",
        mode="ignore",
    )

    assert values.whatsapp_id == WHATSAPP_ID
    assert values.mode is ExclusionMode.IGNORE
    with pytest.raises(ValidationError):
        AutomationExclusionCreate(
            whatsapp_id="120363025000000000@g.us",
            mode="ignore",
        )
    with pytest.raises(ValueError):
        normalize_whatsapp_id("status@broadcast")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ExclusionMode.IGNORE, AutomationDecision.IGNORED),
        (ExclusionMode.HUMAN_ONLY, AutomationDecision.HUMAN_ONLY),
        (None, AutomationDecision.ALLOWED),
    ],
)
async def test_fixed_exclusion_policy_blocks_only_when_active(
    mode: ExclusionMode | None,
    expected: AutomationDecision,
) -> None:
    repository = FakeAutomationRepository()
    if mode is not None:
        repository.exclusions[(BUSINESS_A, WHATSAPP_ID)] = mode
    policy = AutomationPolicyService(repository, now=lambda: NOW)

    active = await policy.active_exclusion(BUSINESS_A, WHATSAPP_ID)
    decision = await policy.evaluate_customer_inbound(
        BUSINESS_A,
        CONVERSATION,
        active,
    )

    assert decision is expected
    assert await policy.active_exclusion(BUSINESS_B, WHATSAPP_ID) is None
    assert bool(repository.human_only_marked) is (
        mode is ExclusionMode.HUMAN_ONLY
    )


@pytest.mark.asyncio
async def test_expired_human_control_resumes_on_next_customer_message_without_job() -> None:
    repository = FakeAutomationRepository()
    current = [NOW]
    policy = AutomationPolicyService(repository, now=lambda: current[0])
    repository.controls[CONVERSATION] = ConversationAutomationControl(
        NOW + timedelta(minutes=30),
        "none",
    )

    assert await policy.evaluate_customer_inbound(
        BUSINESS_A, CONVERSATION, None
    ) is AutomationDecision.TEMPORARILY_SUPPRESSED

    current[0] = NOW + timedelta(minutes=31)
    assert await policy.evaluate_customer_inbound(
        BUSINESS_A, CONVERSATION, None
    ) is AutomationDecision.ALLOWED
    assert repository.suppressions_cleared == [(BUSINESS_A, CONVERSATION)]
    assert repository.controls[CONVERSATION].automation_suppressed_until is None


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", HUMAN_CONTROL_WINDOW_PRESETS)
async def test_manual_message_starts_and_renews_each_allowed_window(
    minutes: int,
) -> None:
    repository = FakeAutomationRepository()
    repository.window_minutes = minutes
    policy = AutomationPolicyService(repository, now=lambda: NOW)

    first_until = await policy.register_manual_business_message(
        BUSINESS_A, CONVERSATION, NOW
    )
    renewed_at = NOW + timedelta(minutes=2)
    renewed_until = await policy.register_manual_business_message(
        BUSINESS_A, CONVERSATION, renewed_at
    )

    assert first_until == NOW + timedelta(minutes=minutes)
    assert renewed_until == renewed_at + timedelta(minutes=minutes)
    assert len(repository.manual_events) == 2
    assert repository.exclusions == {}


class FakeAdministrationRepository:
    def __init__(self) -> None:
        self.exclusion = BusinessAutomationExclusion(
            id=uuid.uuid4(),
            business_id=BUSINESS_A,
            whatsapp_id=WHATSAPP_ID,
            mode="ignore",
            active=True,
        )
        self.added: list[BusinessAutomationExclusion] = []
        self.cancelled: list[tuple[uuid.UUID, str]] = []
        self.windows: list[tuple[uuid.UUID, int]] = []

    async def add_exclusion(self, exclusion: BusinessAutomationExclusion) -> None:
        self.added.append(exclusion)

    async def list_exclusions(
        self, business_id: uuid.UUID
    ) -> list[BusinessAutomationExclusion]:
        return [self.exclusion] if business_id == BUSINESS_A else []

    async def get_exclusion(
        self, business_id: uuid.UUID, exclusion_id: uuid.UUID
    ) -> BusinessAutomationExclusion | None:
        if (
            business_id == self.exclusion.business_id
            and exclusion_id == self.exclusion.id
        ):
            return self.exclusion
        return None

    async def cancel_pending_for_contact(
        self, business_id: uuid.UUID, whatsapp_id: str
    ) -> None:
        self.cancelled.append((business_id, whatsapp_id))

    async def update_business_window(
        self, business_id: uuid.UUID, minutes: int
    ) -> bool:
        self.windows.append((business_id, minutes))
        return business_id == BUSINESS_A

    async def get_business_window(self, business_id: uuid.UUID) -> int | None:
        return 360 if business_id == BUSINESS_A else None


@pytest.mark.asyncio
async def test_future_admin_operations_are_validated_and_business_scoped() -> None:
    repository = FakeAdministrationRepository()
    service = AutomationAdministrationService(repository)
    created = await service.add_exclusion(
        BUSINESS_B,
        AutomationExclusionCreate(
            whatsapp_id=WHATSAPP_ID,
            mode="human_only",
        ),
    )

    assert created.business_id == BUSINESS_B
    assert repository.cancelled == [(BUSINESS_B, WHATSAPP_ID)]
    assert await service.list_exclusions(BUSINESS_A) == [repository.exclusion]
    assert await service.list_exclusions(BUSINESS_B) == []
    assert await service.update_exclusion(
        BUSINESS_B,
        repository.exclusion.id,
        AutomationExclusionUpdate(active=False),
    ) is None

    updated = await service.update_exclusion(
        BUSINESS_A,
        repository.exclusion.id,
        AutomationExclusionUpdate(
            mode="human_only",
            label="Equipe humana",
            active=False,
        ),
    )
    assert updated is repository.exclusion
    assert updated.mode == "human_only"
    assert updated.label == "Equipe humana"
    assert updated.active is False

    assert await service.set_human_control_window(BUSINESS_A, 360) is True
    assert repository.windows == [(BUSINESS_A, 360)]
    effective = await service.get_effective_settings(BUSINESS_A)
    assert effective is not None
    assert effective.business_id == BUSINESS_A
    assert effective.human_control_window_minutes == 360
    assert await service.get_effective_settings(BUSINESS_B) is None
    with pytest.raises(ValueError):
        await service.set_human_control_window(BUSINESS_A, 31)

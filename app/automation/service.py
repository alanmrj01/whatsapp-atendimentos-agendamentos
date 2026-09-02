from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from app.automation.domain import (
    AutomationDecision,
    ExclusionMode,
    normalize_whatsapp_id,
    validate_human_control_window,
)
from app.models import BusinessAutomationExclusion
from app.repositories.automation import ConversationAutomationControl
from app.schemas.automation import (
    AutomationExclusionCreate,
    AutomationExclusionUpdate,
    BusinessAutomationSettings,
)


class AutomationPolicyRepository(Protocol):
    async def get_active_exclusion_mode(
        self, business_id: uuid.UUID, whatsapp_id: str
    ) -> ExclusionMode | None: ...

    async def lock_conversation_control(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ConversationAutomationControl: ...

    async def mark_human_only(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None: ...

    async def clear_human_only_marker(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None: ...

    async def clear_temporary_suppression(
        self, business_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None: ...

    async def activate_human_control(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> datetime: ...


class AutomationPolicyService:
    def __init__(
        self,
        repository: AutomationPolicyRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def active_exclusion(
        self,
        business_id: uuid.UUID,
        whatsapp_id: str,
    ) -> ExclusionMode | None:
        return await self.repository.get_active_exclusion_mode(
            business_id,
            normalize_whatsapp_id(whatsapp_id),
        )

    async def evaluate_customer_inbound(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        exclusion_mode: ExclusionMode | None,
    ) -> AutomationDecision:
        if exclusion_mode is ExclusionMode.IGNORE:
            return AutomationDecision.IGNORED
        if exclusion_mode is ExclusionMode.HUMAN_ONLY:
            await self.repository.mark_human_only(business_id, conversation_id)
            return AutomationDecision.HUMAN_ONLY

        await self.repository.clear_human_only_marker(
            business_id, conversation_id
        )
        control = await self.repository.lock_conversation_control(
            business_id, conversation_id
        )
        suppressed_until = control.automation_suppressed_until
        if suppressed_until is None:
            return AutomationDecision.ALLOWED
        if suppressed_until > self.now():
            return AutomationDecision.TEMPORARILY_SUPPRESSED

        await self.repository.clear_temporary_suppression(
            business_id, conversation_id
        )
        return AutomationDecision.ALLOWED

    async def register_manual_business_message(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> datetime:
        return await self.repository.activate_human_control(
            business_id,
            conversation_id,
            occurred_at,
        )


class AutomationAdministrationService:
    """Validated operations ready to be called by a future authorized API."""

    def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
        self.repository = repository

    async def add_exclusion(
        self,
        business_id: uuid.UUID,
        values: AutomationExclusionCreate,
    ) -> BusinessAutomationExclusion:
        exclusion = BusinessAutomationExclusion(
            business_id=business_id,
            whatsapp_id=values.whatsapp_id,
            mode=values.mode.value,
            label=values.label,
            reason=values.reason,
            active=values.active,
        )
        await self.repository.add_exclusion(exclusion)
        if values.active:
            await self.repository.cancel_pending_for_contact(
                business_id, values.whatsapp_id
            )
        return exclusion

    async def list_exclusions(
        self,
        business_id: uuid.UUID,
    ) -> list[BusinessAutomationExclusion]:
        return await self.repository.list_exclusions(business_id)

    async def update_exclusion(
        self,
        business_id: uuid.UUID,
        exclusion_id: uuid.UUID,
        values: AutomationExclusionUpdate,
    ) -> BusinessAutomationExclusion | None:
        exclusion = await self.repository.get_exclusion(
            business_id, exclusion_id
        )
        if exclusion is None:
            return None
        updates = values.model_dump(exclude_unset=True)
        for key, value in updates.items():
            if key == "mode" and value is not None:
                value = value.value
            setattr(exclusion, key, value)
        if exclusion.active:
            await self.repository.cancel_pending_for_contact(
                business_id, exclusion.whatsapp_id
            )
        return exclusion

    async def set_human_control_window(
        self,
        business_id: uuid.UUID,
        minutes: int,
    ) -> bool:
        return await self.repository.update_business_window(
            business_id,
            validate_human_control_window(minutes),
        )

    async def get_effective_settings(
        self,
        business_id: uuid.UUID,
    ) -> BusinessAutomationSettings | None:
        minutes = await self.repository.get_business_window(business_id)
        if minutes is None:
            return None
        return BusinessAutomationSettings(
            business_id=business_id,
            human_control_window_minutes=minutes,
        )

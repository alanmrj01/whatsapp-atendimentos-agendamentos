from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.domain import ExclusionMode
from app.models import (
    Business,
    BusinessAutomationExclusion,
    Conversation,
    Customer,
    Message,
)


@dataclass(frozen=True, slots=True)
class ConversationAutomationControl:
    automation_suppressed_until: datetime | None
    handoff_status: str


class AutomationRepository:
    """Persistence scoped by business for runtime policy and future admin APIs."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_exclusion_mode(
        self,
        business_id: uuid.UUID,
        whatsapp_id: str,
    ) -> ExclusionMode | None:
        result = await self.session.execute(
            select(BusinessAutomationExclusion.mode).where(
                BusinessAutomationExclusion.business_id == business_id,
                BusinessAutomationExclusion.whatsapp_id == whatsapp_id,
                BusinessAutomationExclusion.active.is_(True),
            )
        )
        mode = result.scalar_one_or_none()
        return ExclusionMode(mode) if mode is not None else None

    async def lock_conversation_control(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> ConversationAutomationControl:
        result = await self.session.execute(
            select(
                Conversation.automation_suppressed_until,
                Conversation.handoff_status,
            )
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
        )
        row = result.one()
        return ConversationAutomationControl(
            automation_suppressed_until=row.automation_suppressed_until,
            handoff_status=row.handoff_status,
        )

    async def mark_human_only(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.handoff_status == "none",
            )
            .values(handoff_status="human_only")
        )
        await self.cancel_pending_outbounds(business_id, conversation_id)

    async def clear_human_only_marker(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.handoff_status == "human_only",
            )
            .values(handoff_status="none")
        )

    async def clear_temporary_suppression(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .values(
                automation_suppressed_until=None,
                suppression_reason=None,
            )
        )

    async def activate_human_control(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> datetime:
        window_result = await self.session.execute(
            select(Business.human_control_window_minutes).where(
                Business.id == business_id
            )
        )
        window_minutes = window_result.scalar_one()
        suppressed_until = occurred_at + _minutes(window_minutes)
        await self.lock_conversation_control(business_id, conversation_id)
        is_latest_human_message = or_(
            Conversation.last_human_message_at.is_(None),
            Conversation.last_human_message_at <= occurred_at,
        )
        result = await self.session.execute(
            update(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .values(
                automation_suppressed_until=case(
                    (is_latest_human_message, suppressed_until),
                    else_=Conversation.automation_suppressed_until,
                ),
                suppression_reason=case(
                    (is_latest_human_message, "manual_business_message"),
                    else_=Conversation.suppression_reason,
                ),
                human_control_started_at=case(
                    (
                        and_(
                            is_latest_human_message,
                            or_(
                                Conversation.automation_suppressed_until.is_(None),
                                Conversation.automation_suppressed_until
                                <= occurred_at,
                            ),
                        ),
                        occurred_at,
                    ),
                    else_=Conversation.human_control_started_at,
                ),
                last_human_message_at=case(
                    (is_latest_human_message, occurred_at),
                    else_=Conversation.last_human_message_at,
                ),
                last_interaction_at=case(
                    (Conversation.last_interaction_at < occurred_at, occurred_at),
                    else_=Conversation.last_interaction_at,
                ),
            )
            .returning(Conversation.automation_suppressed_until)
        )
        await self.cancel_pending_outbounds(business_id, conversation_id)
        return result.scalar_one()

    async def cancel_pending_outbounds(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            update(Message)
            .where(
                Message.business_id == business_id,
                Message.conversation_id == conversation_id,
                Message.direction == "outbound",
                Message.status == "pending",
            )
            .values(status="failed")
        )

    async def list_exclusions(
        self, business_id: uuid.UUID
    ) -> list[BusinessAutomationExclusion]:
        result = await self.session.execute(
            select(BusinessAutomationExclusion)
            .where(BusinessAutomationExclusion.business_id == business_id)
            .order_by(BusinessAutomationExclusion.created_at)
        )
        return list(result.scalars())

    async def get_exclusion(
        self,
        business_id: uuid.UUID,
        exclusion_id: uuid.UUID,
    ) -> BusinessAutomationExclusion | None:
        result = await self.session.execute(
            select(BusinessAutomationExclusion).where(
                BusinessAutomationExclusion.business_id == business_id,
                BusinessAutomationExclusion.id == exclusion_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_exclusion(
        self,
        exclusion: BusinessAutomationExclusion,
    ) -> None:
        self.session.add(exclusion)

    async def delete_exclusion(
        self,
        business_id: uuid.UUID,
        exclusion_id: uuid.UUID,
    ) -> None:
        await self.session.execute(
            delete(BusinessAutomationExclusion).where(
                BusinessAutomationExclusion.business_id == business_id,
                BusinessAutomationExclusion.id == exclusion_id,
            )
        )

    async def update_business_window(
        self,
        business_id: uuid.UUID,
        minutes: int,
    ) -> bool:
        result = await self.session.execute(
            update(Business)
            .where(Business.id == business_id)
            .values(human_control_window_minutes=minutes)
            .returning(Business.id)
        )
        return result.scalar_one_or_none() is not None

    async def get_business_window(
        self,
        business_id: uuid.UUID,
    ) -> int | None:
        result = await self.session.execute(
            select(Business.human_control_window_minutes).where(
                Business.id == business_id
            )
        )
        return result.scalar_one_or_none()

    async def cancel_pending_for_contact(
        self,
        business_id: uuid.UUID,
        whatsapp_id: str,
    ) -> None:
        await self.session.execute(
            update(Message)
            .where(
                Message.business_id == business_id,
                Message.direction == "outbound",
                Message.status == "pending",
                Message.conversation_id.in_(
                    select(Conversation.id)
                    .join(
                        Customer,
                        and_(
                            Customer.business_id == Conversation.business_id,
                            Customer.id == Conversation.customer_id,
                        ),
                    )
                    .where(
                        Conversation.business_id == business_id,
                        Customer.whatsapp_id == whatsapp_id,
                    )
                ),
            )
            .values(status="failed")
        )


def _minutes(value: int) -> timedelta:
    return timedelta(minutes=value)

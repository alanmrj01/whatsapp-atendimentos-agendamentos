from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


CommercialAutomationType = Literal[
    "abandoned_followup_24h",
    "cleaning_reminder_6m",
]
CommercialAutomationStatus = Literal[
    "queued",
    "sent",
    "responded",
    "declined",
    "accepted",
    "handoff",
    "skipped",
    "failed",
]


class CommercialAutomationView(BaseModel):
    id: UUID
    event_type: CommercialAutomationType
    status: CommercialAutomationStatus
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    appointment_id: UUID | None
    result_appointment_id: UUID | None
    due_at: datetime
    sent_at: datetime | None
    resolved_at: datetime | None
    source_service_name: str | None = None
    source_service_date: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class CommercialAutomationList(BaseModel):
    items: list[CommercialAutomationView]

    model_config = ConfigDict(extra="forbid")


class CleaningReminderUpcomingView(BaseModel):
    customer_id: UUID
    customer_name: str
    customer_phone: str | None
    source_appointment_id: UUID
    source_service_name: str
    source_service_date: datetime
    due_at: datetime

    model_config = ConfigDict(extra="forbid")


class CleaningReminderDashboard(BaseModel):
    upcoming: list[CleaningReminderUpcomingView]
    history: list[CommercialAutomationView]

    model_config = ConfigDict(extra="forbid")

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReengagementPromptResponse(BaseModel):
    id: UUID
    campaign: Literal["upgrade", "whatsapp_activation"]
    step: int = Field(ge=1, le=3)
    title: str
    body: str
    cta_label: str
    cta_path: str

    model_config = ConfigDict(extra="forbid")


class ReengagementSweepPayload(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)

    model_config = ConfigDict(extra="forbid")

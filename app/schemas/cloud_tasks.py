from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppEventTaskPayload(BaseModel):
    event_key: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^whatsapp:(?:inbound|status):[a-f0-9]{64}$",
    )

    model_config = ConfigDict(extra="forbid")


class TaskAcknowledgement(BaseModel):
    status: Literal["accepted"]

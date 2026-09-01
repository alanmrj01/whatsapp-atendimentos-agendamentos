from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppWebhookPayload(BaseModel):
    object: Literal["whatsapp_business_account"]
    entry: list[dict[str, Any]] = Field(min_length=1)

    model_config = ConfigDict(extra="allow")


class WebhookAcknowledgement(BaseModel):
    status: Literal["accepted"]

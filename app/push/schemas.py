from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class PushSubscriptionKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=16, max_length=512)
    auth: str = Field(min_length=8, max_length=256)


class PushSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl = Field(max_length=4096)
    keys: PushSubscriptionKeys


class WebPushConfigResponse(BaseModel):
    enabled: bool
    public_key: str | None = None


class WebPushSubscriptionResponse(BaseModel):
    subscribed: bool

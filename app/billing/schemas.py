from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PlanCode = Literal["basic", "plus"]
BillingCycle = Literal["monthly", "quarterly", "annual"]


class StrictBillingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutCreateRequest(StrictBillingRequest):
    plan: PlanCode
    cycle: BillingCycle
    return_origin: str = Field(min_length=8, max_length=300)


class CheckoutCreateResponse(BaseModel):
    checkout_id: UUID
    checkout_url: str
    plan: PlanCode
    cycle: BillingCycle
    amount_cents: int


class CheckoutStatusResponse(BaseModel):
    checkout_id: UUID
    status: Literal["creating", "active", "paid", "canceled", "expired", "failed"]
    plan: PlanCode
    cycle: BillingCycle


class SubscriptionStatusResponse(BaseModel):
    status: Literal["none", "active", "past_due", "canceled", "suspended"]
    plan: PlanCode | None = None
    cycle: BillingCycle | None = None
    access_until: datetime | None = None

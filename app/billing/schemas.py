from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PlanCode = Literal["basic", "plus"]
BillingCycle = Literal["monthly", "quarterly", "annual"]
PaymentMethod = Literal["credit_card", "pix_automatic"]


class StrictBillingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutCreateRequest(StrictBillingRequest):
    plan: PlanCode
    cycle: BillingCycle
    payment_method: PaymentMethod = "credit_card"
    return_origin: str = Field(min_length=8, max_length=300)
    payer_name: str | None = Field(default=None, min_length=2, max_length=120)
    payer_cpf_cnpj: str | None = Field(default=None, max_length=18)

    @field_validator("payer_name", mode="before")
    @classmethod
    def normalize_payer_name(cls, value):
        if value is None:
            return None
        return " ".join(str(value).strip().split()) or None

    @field_validator("payer_cpf_cnpj", mode="before")
    @classmethod
    def normalize_document(cls, value):
        if value is None:
            return None
        digits = re.sub(r"\D", "", str(value))
        if digits and len(digits) not in {11, 14}:
            raise ValueError("CPF/CNPJ must have 11 or 14 digits")
        return digits or None

    @model_validator(mode="after")
    def require_pix_payer(self):
        if self.payment_method == "pix_automatic" and (
            not self.payer_name or not self.payer_cpf_cnpj
        ):
            raise ValueError("Pix Automatic requires payer name and CPF/CNPJ")
        return self


class CheckoutCreateResponse(BaseModel):
    checkout_id: UUID
    payment_method: PaymentMethod
    checkout_url: str | None = None
    pix_authorization_id: str | None = None
    pix_payload: str | None = None
    pix_expires_at: datetime | None = None
    plan: PlanCode
    cycle: BillingCycle
    amount_cents: int


class CheckoutStatusResponse(BaseModel):
    checkout_id: UUID
    status: Literal["creating", "active", "paid", "canceled", "expired", "failed"]
    payment_method: PaymentMethod
    plan: PlanCode
    cycle: BillingCycle


class SubscriptionStatusResponse(BaseModel):
    status: Literal["none", "active", "past_due", "canceled", "suspended"]
    payment_method: PaymentMethod | None = None
    plan: PlanCode | None = None
    cycle: BillingCycle | None = None
    access_until: datetime | None = None

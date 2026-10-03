from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


PlanCode = Literal["basic", "plus"]
BillingCycle = Literal["monthly", "quarterly", "annual"]
PaymentMethod = Literal["credit_card", "pix_automatic"]
CheckoutMode = Literal["hosted", "native", "pix"]


class StrictBillingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


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
    checkout_mode: CheckoutMode
    checkout_url: str | None = None
    pix_authorization_id: str | None = None
    pix_payload: str | None = None
    pix_expires_at: datetime | None = None
    expires_at: datetime | None = None
    plan: PlanCode
    cycle: BillingCycle
    amount_cents: int


class CheckoutStatusResponse(BaseModel):
    checkout_id: UUID
    status: Literal["creating", "active", "paid", "canceled", "expired", "failed"]
    payment_method: PaymentMethod
    plan: PlanCode
    cycle: BillingCycle
    expires_at: datetime | None = None


class CheckoutProfileResponse(BaseModel):
    email: str
    business_name: str
    payer_name: str | None = None
    postal_code: str | None = None
    address_number: str | None = None


class CreditCardCheckoutRequest(StrictBillingRequest):
    payer_name: str = Field(min_length=2, max_length=120)
    payer_cpf_cnpj: str = Field(min_length=11, max_length=18)
    payer_postal_code: str = Field(min_length=8, max_length=9)
    payer_address_number: str = Field(min_length=1, max_length=32)
    payer_address_complement: str | None = Field(default=None, max_length=80)
    payer_phone: str = Field(min_length=10, max_length=20)
    card_holder_name: str = Field(min_length=2, max_length=120)
    card_number: SecretStr = Field(min_length=13, max_length=24)
    card_expiry_month: str = Field(min_length=1, max_length=2)
    card_expiry_year: str = Field(min_length=2, max_length=4)
    card_ccv: SecretStr = Field(min_length=3, max_length=4)

    @field_validator("payer_name", "card_holder_name", mode="before")
    @classmethod
    def normalize_names(cls, value):
        return " ".join(str(value).strip().split())

    @field_validator("payer_cpf_cnpj", mode="before")
    @classmethod
    def normalize_payer_document(cls, value):
        digits = re.sub(r"\D", "", str(value))
        if len(digits) not in {11, 14}:
            raise ValueError("CPF/CNPJ must have 11 or 14 digits")
        return digits

    @field_validator("payer_postal_code", mode="before")
    @classmethod
    def normalize_postal_code(cls, value):
        digits = re.sub(r"\D", "", str(value))
        if len(digits) != 8:
            raise ValueError("Postal code must have 8 digits")
        return digits

    @field_validator("payer_phone", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        digits = re.sub(r"\D", "", str(value))
        if len(digits) not in {10, 11}:
            raise ValueError("Phone must have 10 or 11 digits")
        return digits

    @field_validator("payer_address_number", mode="before")
    @classmethod
    def normalize_address_number(cls, value):
        return str(value).strip()

    @field_validator("payer_address_complement", mode="before")
    @classmethod
    def normalize_address_complement(cls, value):
        if value is None:
            return None
        normalized = " ".join(str(value).strip().split())
        return normalized or None

    @field_validator("card_number", mode="before")
    @classmethod
    def normalize_card_number(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        digits = re.sub(r"\D", "", raw)
        if not 13 <= len(digits) <= 19 or not _luhn_valid(digits):
            raise ValueError("Invalid card number")
        return digits

    @field_validator("card_ccv", mode="before")
    @classmethod
    def normalize_card_ccv(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        digits = re.sub(r"\D", "", raw)
        if len(digits) not in {3, 4}:
            raise ValueError("Invalid card security code")
        return digits

    @field_validator("card_expiry_month", mode="before")
    @classmethod
    def normalize_expiry_month(cls, value):
        raw = re.sub(r"\D", "", str(value))
        if not raw:
            raise ValueError("Invalid expiry month")
        month = int(raw)
        if not 1 <= month <= 12:
            raise ValueError("Invalid expiry month")
        return f"{month:02d}"

    @field_validator("card_expiry_year", mode="before")
    @classmethod
    def normalize_expiry_year(cls, value):
        raw = re.sub(r"\D", "", str(value))
        if len(raw) == 2:
            raw = f"20{raw}"
        if len(raw) != 4:
            raise ValueError("Invalid expiry year")
        return raw


class CreditCardCheckoutResponse(BaseModel):
    checkout_id: UUID
    status: Literal["creating", "active", "paid", "failed"]
    expires_at: datetime | None = None


class SubscriptionStatusResponse(BaseModel):
    status: Literal["none", "active", "past_due", "canceled", "suspended"]
    payment_method: PaymentMethod | None = None
    plan: PlanCode | None = None
    cycle: BillingCycle | None = None
    access_until: datetime | None = None

def _luhn_valid(number: str) -> bool:
    total = 0
    parity = len(number) % 2
    for index, digit in enumerate(number):
        value = int(digit)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0

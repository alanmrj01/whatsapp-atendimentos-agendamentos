from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal

PlanCode = Literal["basic", "plus"]
BillingCycle = Literal["monthly", "quarterly", "annual"]

ASAAS_RECURRENT_MIN_AMOUNT_CENTS = 500
_SANDBOX_TEST_MAX_AMOUNT_CENTS = 100_000


class BillingCatalogConfigurationError(RuntimeError):
    """Invalid server-side billing catalog configuration."""


@dataclass(frozen=True, slots=True)
class Offer:
    plan: PlanCode
    cycle: BillingCycle
    plan_name: str
    amount_cents: int
    asaas_cycle: str
    asaas_pix_frequency: str
    users: int
    automatic_attendances: int


_PLAN = {
    "basic": {"name": "Basic", "monthly_cents": 19_700, "users": 1, "attendances": 500},
    "plus": {"name": "Plus", "monthly_cents": 29_700, "users": 5, "attendances": 1_500},
}

_CYCLE = {
    "monthly": {
        "months": 1,
        "discount_bps": 0,
        "asaas": "MONTHLY",
        "pix_frequency": "MONTHLY",
    },
    "quarterly": {
        "months": 3,
        "discount_bps": 1_000,
        "asaas": "QUARTERLY",
        "pix_frequency": "QUARTERLY",
    },
    "annual": {
        "months": 12,
        "discount_bps": 1_500,
        "asaas": "YEARLY",
        "pix_frequency": "ANNUALLY",
    },
}


def _sandbox_test_amount_cents() -> int | None:
    if os.getenv("BILLING_PROVIDER_ENVIRONMENT", "production").strip().lower() != "sandbox":
        return None
    raw = os.getenv("BILLING_TEST_AMOUNT_CENTS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise BillingCatalogConfigurationError(
            "Invalid sandbox billing test amount"
        ) from None
    if not ASAAS_RECURRENT_MIN_AMOUNT_CENTS <= value <= _SANDBOX_TEST_MAX_AMOUNT_CENTS:
        raise BillingCatalogConfigurationError("Invalid sandbox billing test amount")
    return value


def get_offer(plan: str, cycle: str) -> Offer:
    if plan not in _PLAN or cycle not in _CYCLE:
        raise ValueError("Unknown commercial offer")
    plan_data = _PLAN[plan]
    cycle_data = _CYCLE[cycle]
    gross = plan_data["monthly_cents"] * cycle_data["months"]
    amount = gross * (10_000 - cycle_data["discount_bps"]) // 10_000
    test_amount = _sandbox_test_amount_cents()
    if test_amount is not None:
        amount = test_amount
    return Offer(
        plan=plan,  # type: ignore[arg-type]
        cycle=cycle,  # type: ignore[arg-type]
        plan_name=plan_data["name"],
        amount_cents=amount,
        asaas_cycle=cycle_data["asaas"],
        asaas_pix_frequency=cycle_data["pix_frequency"],
        users=plan_data["users"],
        automatic_attendances=plan_data["attendances"],
    )


def cycle_months(cycle: BillingCycle) -> int:
    return int(_CYCLE[cycle]["months"])

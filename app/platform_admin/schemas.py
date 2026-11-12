from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.auth.schemas import AccessMode, StrictRequest
from app.auth.security import normalize_email


class PlatformBusinessCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="America/Sao_Paulo", min_length=1, max_length=64)
    owner_email: str = Field(max_length=254)
    owner_password: SecretStr = Field(min_length=12, max_length=1024)

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("Business name is required")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone") from None
        return value

    @field_validator("owner_email")
    @classmethod
    def normalized_owner_email(cls, value: str) -> str:
        return normalize_email(value)


class PlatformBusinessStatusRequest(StrictRequest):
    active: bool = Field(strict=True)


class PlatformBusinessAccessRequest(StrictRequest):
    access_mode: AccessMode


class PlatformBusinessResponse(BaseModel):
    id: UUID
    name: str
    timezone: str
    active: bool
    # Administrative/legacy access flag. Commercial subscription state is
    # reported separately so the admin UI never confuses billing with an override.
    access_mode: AccessMode = "paid"
    admin_full_access: bool = False
    commercial_access_active: bool = False
    commercial_plan_code: Literal["basic", "plus"] | None = None
    commercial_billing_cycle: Literal["monthly", "quarterly", "annual"] | None = None
    commercial_subscription_status: Literal[
        "active", "past_due", "canceled", "suspended"
    ] | None = None
    commercial_access_until: datetime | None = None
    commercial_cancellation_requested_at: datetime | None = None
    commercial_refund_status: Literal[
        "none", "requested", "reconcile_required", "refunded"
    ] | None = None
    commercial_refund_requested_at: datetime | None = None
    commercial_refunded_at: datetime | None = None
    commercial_provider_payment_id: str | None = None
    commercial_provider_subscription_id: str | None = None
    commercial_contact_name: str | None = None
    commercial_contact_email: str | None = None
    commercial_contact_phone: str | None = None
    commercial_refund_deadline_at: datetime | None = None
    owners: list[str]
    whatsapp_status: Literal["disconnected", "pending", "connected", "error"]


class PlatformBusinessListResponse(BaseModel):
    businesses: list[PlatformBusinessResponse]


class PlatformBusinessStatusResponse(BaseModel):
    id: UUID
    active: bool


class PlatformBusinessAccessResponse(BaseModel):
    business_id: UUID
    access_mode: AccessMode


class PlatformAnalyticsFunnelStep(BaseModel):
    key: str
    label: str
    value: int | None = None
    conversion_rate: float | None = None
    status: Literal["ready", "awaiting_integration", "insufficient_data"] = "ready"
    note: str | None = None


class PlatformAnalyticsKpi(BaseModel):
    key: str
    label: str
    value: int | float | None = None
    format: Literal["count", "currency_brl", "percent", "ratio"] = "count"
    status: Literal[
        "ready",
        "structuring",
        "awaiting_integration",
        "awaiting_history",
        "insufficient_data",
    ] = "ready"
    note: str | None = None


class PlatformAnalyticsBreakdownItem(BaseModel):
    key: str
    label: str
    value: int


class PlatformAnalyticsBreakdown(BaseModel):
    key: str
    label: str
    status: Literal["ready", "awaiting_integration", "insufficient_data"] = "ready"
    note: str | None = None
    items: list[PlatformAnalyticsBreakdownItem] = Field(default_factory=list)


class PlatformAnalyticsConversionBreakdownItem(BaseModel):
    key: str
    label: str
    quiz_visitors: int = 0
    accounts: int = 0
    subscriptions: int = 0
    activations: int = 0
    revenue_brl: float = 0


class PlatformAnalyticsConversionBreakdown(BaseModel):
    key: str
    label: str
    status: Literal["ready", "awaiting_integration", "insufficient_data"] = "ready"
    note: str | None = None
    items: list[PlatformAnalyticsConversionBreakdownItem] = Field(default_factory=list)


class PlatformAnalyticsQuizAnswerItem(BaseModel):
    answer_id: str
    profile_dimension: str
    responses: int
    share: float | None = None
    accounts: int = 0
    account_conversion_rate: float | None = None
    subscriptions: int = 0
    purchase_conversion_rate: float | None = None


class PlatformAnalyticsQuizQuestion(BaseModel):
    question_id: str
    label: str
    responses: int
    answers: list[PlatformAnalyticsQuizAnswerItem] = Field(default_factory=list)


class PlatformAnalyticsAdPerformanceItem(BaseModel):
    campaign_id: str | None = None
    campaign_name: str | None = None
    adset_id: str | None = None
    adset_name: str | None = None
    ad_id: str
    ad_name: str
    spend_brl: float | None = None
    impressions: int | None = None
    reach: int | None = None
    link_clicks: int | None = None
    ctr: float | None = None
    cpc: float | None = None
    cpm: float | None = None
    quiz_visitors: int = 0
    quiz_completed: int = 0
    accounts: int = 0
    subscriptions: int = 0
    activations: int = 0
    revenue_brl: float = 0
    account_conversion_rate: float | None = None
    cac_brl: float | None = None
    roas: float | None = None


class PlatformAnalyticsLeadRecoveryItem(BaseModel):
    business_id: UUID
    lead_name: str
    whatsapp_e164: str
    business_name: str
    created_at: datetime
    last_activity_at: datetime
    stage_key: Literal[
        "account",
        "demo",
        "whatsapp_connected_unpaid",
        "offer",
        "checkout",
        "payment_failed",
        "purchase",
        "activated",
        "refund_requested",
        "refunded",
    ]
    stage_label: str
    purchased: bool = False
    activated: bool = False
    needs_follow_up: bool = True
    angle: str | None = None
    quiz_profile: str | None = None
    ad_id: str | None = None
    ad_name: str | None = None


class PlatformAnalyticsSeriesPoint(BaseModel):
    date: date
    # Backward-compatible aliases for the previous dashboard.
    lpv: int = 0
    vsl: int = 0
    quiz_views: int = 0
    quiz_completed: int = 0
    vsl_started: int = 0
    vsl_completed: int = 0
    accounts: int = 0
    demo_entries: int = 0
    checkout_started: int = 0
    subscriptions: int = 0
    activations: int = 0


class PlatformAnalyticsSummaryResponse(BaseModel):
    generated_at: datetime
    period_days: int
    period_start: datetime
    period_end: datetime
    funnel: list[PlatformAnalyticsFunnelStep]
    kpis: list[PlatformAnalyticsKpi]
    subscriptions_by_plan: PlatformAnalyticsBreakdown
    subscriptions_by_payment_method: PlatformAnalyticsBreakdown
    subscriptions_by_placement: PlatformAnalyticsBreakdown
    conversions_by_angle: PlatformAnalyticsConversionBreakdown
    conversions_by_quiz_profile: PlatformAnalyticsConversionBreakdown
    onboarding_issues: PlatformAnalyticsBreakdown
    quiz_questions: list[PlatformAnalyticsQuizQuestion]
    ad_performance: list[PlatformAnalyticsAdPerformanceItem]
    lead_recovery: list[PlatformAnalyticsLeadRecoveryItem]
    meta_ads_status: Literal["ready", "awaiting_integration"]
    meta_ads_note: str | None = None
    series: list[PlatformAnalyticsSeriesPoint]
    future_metrics: list[PlatformAnalyticsKpi]

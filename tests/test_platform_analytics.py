from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.analytics.meta_ads import MetaAdInsight, MetaAdsReport
from app.platform_admin.analytics import (
    ActiveSubscriptionRow,
    AnalyticsEventRow,
    LeadContactRow,
    PaidCheckoutRow,
    RefundRow,
    build_platform_analytics_summary,
)


NOW = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
VISITOR = UUID("00000000-0000-0000-0000-000000000101")
BUSINESS = UUID("00000000-0000-0000-0000-000000000102")
USER = UUID("00000000-0000-0000-0000-000000000103")
CHECKOUT = UUID("00000000-0000-0000-0000-000000000104")


def event(
    name: str,
    *,
    minutes: int,
    properties: dict | None = None,
    business_id: UUID | None = None,
) -> AnalyticsEventRow:
    return AnalyticsEventRow(
        visitor_id=VISITOR,
        business_id=business_id,
        user_id=USER if business_id else None,
        event_name=name,
        occurred_at=NOW - timedelta(minutes=minutes),
        properties=properties,
    )


def acquisition() -> dict[str, str]:
    return {
        "utm_source": "fb",
        "utm_medium": "paid_social",
        "utm_campaign": "ALOVIA | SALES | TESTE01",
        "utm_content": "C01 | DOR | CLIENTE_ESPERANDO",
        "utm_term": "CLIENTE_ESPERANDO | BROAD | BR | CR",
        "campaign_id": "9001",
        "adset_id": "9002",
        "ad_id": "9003",
        "placement": "instagram_stories",
        "angle": "cliente_esperando_whatsapp",
        "quiz_profile": "at_risk",
    }


def base_events() -> list[AnalyticsEventRow]:
    touch = acquisition()
    return [
        event("quiz_viewed", minutes=20, properties={**touch, "funnel_stage": "quiz_viewed"}),
        # A duplicate page load must not become another lead.
        event("quiz_viewed", minutes=19, properties={**touch, "funnel_stage": "quiz_viewed"}),
        event(
            "quiz_answered",
            minutes=18,
            properties={
                **touch,
                "question_id": "q1",
                "answer_id": "C",
                "question_number": 1,
                "profile_dimension": "at_risk",
            },
        ),
        event(
            "quiz_completed",
            minutes=17,
            properties={**touch, "result_profile": "at_risk"},
        ),
        event(
            "quiz_result_viewed",
            minutes=16,
            properties={**touch, "result_profile": "at_risk"},
        ),
        event(
            "quiz_demo_started",
            minutes=15,
            properties={**touch, "video_id": "1240828681"},
        ),
        event(
            "quiz_demo_completed",
            minutes=14,
            properties={**touch, "percent": 100},
        ),
        event(
            "quiz_signup_cta_clicked",
            minutes=13,
            properties={**touch, "funnel_stage": "signup_cta"},
        ),
        event(
            "signup_completed",
            minutes=12,
            properties={**touch, "funnel_stage": "registration_completed"},
            business_id=BUSINESS,
        ),
        event(
            "preactivation_started",
            minutes=11,
            properties={**touch, "funnel_stage": "demo_access_ready"},
            business_id=BUSINESS,
        ),
        event(
            "activation_paywall_viewed",
            minutes=10,
            properties={**touch, "funnel_stage": "activation_paywall"},
            business_id=BUSINESS,
        ),
        event(
            "checkout_started",
            minutes=9,
            properties={**touch, "funnel_stage": "checkout_started"},
            business_id=BUSINESS,
        ),
        event(
            "whatsapp_connection_completed",
            minutes=1,
            properties={**touch, "funnel_stage": "whatsapp_connected"},
            business_id=BUSINESS,
        ),
    ]


def paid_checkout(*, refunded: bool = False) -> PaidCheckoutRow:
    return PaidCheckoutRow(
        checkout_id=CHECKOUT,
        business_id=BUSINESS,
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        amount_cents=14_700,
        paid_at=NOW - timedelta(minutes=5),
        refund_status="refunded" if refunded else "none",
        refund_requested_at=NOW - timedelta(minutes=3) if refunded else None,
        refunded_at=NOW - timedelta(minutes=2) if refunded else None,
    )


def meta_report() -> MetaAdsReport:
    return MetaAdsReport(
        status="ready",
        note=None,
        ads=[
            MetaAdInsight(
                campaign_id="9001",
                campaign_name="ALOVIA | SALES | TESTE01",
                adset_id="9002",
                adset_name="CLIENTE_ESPERANDO | BROAD | BR | CR",
                ad_id="9003",
                ad_name="C01 | DOR | CLIENTE_ESPERANDO",
                impressions=1000,
                reach=850,
                link_clicks=50,
                spend_brl=70.0,
                ctr=5.0,
                cpc=1.4,
                cpm=70.0,
            )
        ],
    )


def metric(summary, key: str):
    return next(item for item in summary.kpis if item.key == key)


def test_dashboard_uses_unique_leads_and_current_quiz_funnel() -> None:
    summary = build_platform_analytics_summary(
        events=base_events(),
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[
            ActiveSubscriptionRow(
                billing_cycle="monthly",
                amount_cents=14_700,
                status="active",
                access_until=NOW + timedelta(days=30),
            )
        ],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={BUSINESS: NOW - timedelta(minutes=1)},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}
    assert funnel["ad_clicks"].value == 50
    assert funnel["quiz_views"].value == 1
    assert funnel["quiz_completed"].value == 1
    assert funnel["vsl_started"].value == 1
    assert funnel["accounts"].value == 1
    assert funnel["demo_entries"].value == 1
    assert funnel["checkout_started"].value == 1
    assert funnel["subscriptions"].value == 1
    assert funnel["activations"].value == 1

    assert metric(summary, "ad_spend").value == 70.0
    assert metric(summary, "cac").value == 70.0
    assert metric(summary, "roas").value == 2.1
    assert metric(summary, "net_revenue").value == 147.0
    assert metric(summary, "mrr").value == 147.0

    assert summary.conversions_by_angle.items[0].key == "cliente_esperando_whatsapp"
    assert summary.conversions_by_angle.items[0].subscriptions == 1
    assert summary.conversions_by_quiz_profile.items[0].key == "at_risk"
    assert summary.conversions_by_quiz_profile.items[0].subscriptions == 1

    assert len(summary.quiz_questions) == 1
    answer = summary.quiz_questions[0].answers[0]
    assert answer.answer_id == "C"
    assert answer.responses == 1
    assert answer.accounts == 1
    assert answer.subscriptions == 1

    ad = summary.ad_performance[0]
    assert ad.ad_id == "9003"
    assert ad.quiz_visitors == 1
    assert ad.accounts == 1
    assert ad.subscriptions == 1
    assert ad.activations == 1
    assert ad.cac_brl == 70.0
    assert ad.roas == 2.1


def test_refund_is_visible_and_removed_from_net_revenue() -> None:
    checkout = paid_checkout(refunded=True)
    summary = build_platform_analytics_summary(
        events=base_events(),
        paid_checkouts=[checkout],
        refunds=[
            RefundRow(
                checkout_id=CHECKOUT,
                business_id=BUSINESS,
                amount_cents=14_700,
                refunded_at=NOW - timedelta(minutes=2),
            )
        ],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    assert metric(summary, "revenue_received").value == 147.0
    assert metric(summary, "refunds").value == 147.0
    assert metric(summary, "net_revenue").value == 0.0
    assert metric(summary, "roas").value == 0.0
    assert summary.ad_performance[0].revenue_brl == 0.0


def test_lead_recovery_prioritizes_unpaid_and_reports_furthest_stage() -> None:
    events = base_events() + [
        event(
            "payment_failed",
            minutes=2,
            properties={**acquisition(), "reason": "provider_declined"},
            business_id=BUSINESS,
        )
    ]
    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={},
        meta_report=MetaAdsReport(
            status="awaiting_integration",
            note="Aguardando credencial de leitura da Meta Ads.",
            ads=[],
        ),
        now=NOW,
        days=7,
        lead_contacts=[
            LeadContactRow(
                business_id=BUSINESS,
                lead_name="João da Silva",
                whatsapp_e164="+5512999991234",
                business_name="João Refrigeração",
                created_at=NOW - timedelta(minutes=12),
            )
        ],
        connection_at_by_business={BUSINESS: NOW - timedelta(minutes=1)},
    )

    assert len(summary.lead_recovery) == 1
    lead = summary.lead_recovery[0]
    assert lead.lead_name == "João da Silva"
    assert lead.whatsapp_e164 == "+5512999991234"
    assert lead.business_name == "João Refrigeração"
    assert lead.stage_key == "payment_failed"
    assert lead.stage_label == "Pagamento não concluído"
    assert lead.purchased is False
    assert lead.activated is False
    assert lead.needs_follow_up is True
    assert lead.angle == "cliente_esperando_whatsapp"
    assert lead.quiz_profile == "at_risk"
    assert lead.ad_id == "9003"


def test_meta_metrics_remain_explicit_when_integration_is_not_configured() -> None:
    summary = build_platform_analytics_summary(
        events=base_events(),
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={},
        meta_report=MetaAdsReport(
            status="awaiting_integration",
            note="Aguardando credencial de leitura da Meta Ads.",
            ads=[],
        ),
        now=NOW,
        days=7,
    )

    assert summary.meta_ads_status == "awaiting_integration"
    assert metric(summary, "ad_spend").value is None
    assert metric(summary, "ad_spend").status == "awaiting_integration"
    assert summary.funnel[0].value is None
    assert summary.funnel[0].status == "awaiting_integration"


def test_lead_recovery_puts_requested_refund_above_other_opportunities() -> None:
    other_business = UUID("00000000-0000-0000-0000-000000000105")
    requested_refund = PaidCheckoutRow(
        checkout_id=CHECKOUT,
        business_id=BUSINESS,
        plan_code="basic",
        billing_cycle="monthly",
        payment_method="pix",
        amount_cents=14_700,
        paid_at=NOW - timedelta(days=20),
        refund_status="requested",
        refund_requested_at=NOW - timedelta(minutes=1),
        refunded_at=None,
    )
    summary = build_platform_analytics_summary(
        events=[],
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={},
        activation_at_by_business={},
        meta_report=MetaAdsReport(
            status="awaiting_integration",
            note="Aguardando credencial de leitura da Meta Ads.",
            ads=[],
        ),
        now=NOW,
        days=1,
        lead_contacts=[
            LeadContactRow(
                business_id=other_business,
                lead_name="Lead recente",
                whatsapp_e164="+5511999990001",
                business_name="Lead Recente Refrigeração",
                created_at=NOW,
            ),
            LeadContactRow(
                business_id=BUSINESS,
                lead_name="Cliente com reembolso",
                whatsapp_e164="+5511999990002",
                business_name="Cliente Reembolso Refrigeração",
                created_at=NOW - timedelta(days=30),
            ),
        ],
        connection_at_by_business={},
        lead_recovery_checkouts=[requested_refund],
    )

    assert next(item for item in summary.funnel if item.key == "subscriptions").value == 0
    assert metric(summary, "revenue_received").value == 0.0
    assert summary.lead_recovery[0].business_id == BUSINESS
    assert summary.lead_recovery[0].stage_key == "refund_requested"
    assert summary.lead_recovery[0].stage_label == "Reembolso solicitado"
    assert summary.lead_recovery[0].needs_follow_up is True

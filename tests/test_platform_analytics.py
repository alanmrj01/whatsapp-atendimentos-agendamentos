from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.analytics.meta_ads import MetaAdInsight, MetaAdsReport
from app.core.config import Settings
from app.platform_admin.analytics import (
    ActiveSubscriptionRow,
    AnalyticsEventRow,
    LeadContactRow,
    PaidCheckoutRow,
    RefundRow,
    _effective_period_start,
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


def test_meta_quiz_views_ignore_unattributed_views_without_deleting_history() -> None:
    other_visitor = UUID("00000000-0000-0000-0000-000000000199")
    events = base_events() + [
        AnalyticsEventRow(
            visitor_id=other_visitor,
            business_id=None,
            user_id=None,
            event_name="quiz_viewed",
            occurred_at=NOW - timedelta(minutes=6),
            properties={"funnel_stage": "quiz_viewed"},
        )
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={},
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}
    assert funnel["quiz_views"].value == 1
    assert funnel["quiz_views"].label == "Quiz aberto (Meta)"
    assert "2 visitante(s)" in (funnel["quiz_views"].note or "")
    assert metric(summary, "quiz_views").value == 1
    assert "2 visitante(s)" in (metric(summary, "quiz_views").note or "")


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



def test_platform_analytics_start_at_never_expands_requested_window() -> None:
    configured = NOW - timedelta(days=2)
    settings = Settings(
        ENVIRONMENT="production",
        PLATFORM_ANALYTICS_START_AT=configured.isoformat(),
    )

    assert _effective_period_start(settings, now=NOW, days=7) == configured
    assert _effective_period_start(settings, now=NOW, days=1) == NOW - timedelta(days=1)


def test_platform_analytics_start_at_ignores_naive_timestamp() -> None:
    settings = Settings(
        ENVIRONMENT="production",
        PLATFORM_ANALYTICS_START_AT="2026-11-11T15:00:00",
    )

    assert _effective_period_start(settings, now=NOW, days=7) == NOW - timedelta(days=7)


def test_demo_entries_only_count_new_accounts() -> None:
    old_business = UUID("00000000-0000-0000-0000-000000000199")

    events = base_events() + [
        event("demo_page_view", minutes=8, business_id=BUSINESS),
        event("demo_page_view", minutes=7, business_id=old_business),
        event("preactivation_started", minutes=6, business_id=old_business),
        event("checkout_started", minutes=5, business_id=old_business),
        event("demo_page_view", minutes=4),
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={
            BUSINESS: NOW - timedelta(minutes=1)
        },
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}

    assert funnel["accounts"].value == 1
    assert funnel["demo_entries"].value == 1
    assert funnel["demo_entries"].conversion_rate == 100.0
    assert metric(summary, "demo_entries").value == 1
    assert sum(point.demo_entries for point in summary.series) == 1

    assert funnel["checkout_started"].value == 2
    assert funnel["checkout_started"].conversion_rate == 100.0
    assert metric(summary, "checkout_started").value == 2
    assert metric(summary, "subscriptions").value == 1
    assert metric(summary, "net_revenue").value == 147.0
    assert funnel["activations"].value == 1


def test_ad_performance_tracks_individual_funnel_stages() -> None:
    touch = acquisition()
    other_touch = {**touch, "ad_id": "9004"}

    events = base_events() + [
        event("quiz_started", minutes=18, properties=touch),
        event("quiz_started", minutes=17, properties=touch),
        event("quiz_demo_started", minutes=10, properties=touch),
        event("quiz_demo_completed", minutes=9, properties=touch),
        event("quiz_started", minutes=8, properties=other_touch),
        event("quiz_started", minutes=7),
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: touch},
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    ads = {item.ad_id: item for item in summary.ad_performance}

    ad = ads["9003"]
    assert ad.quiz_visitors == 1
    assert ad.quiz_started == 1
    assert ad.quiz_completed == 1
    assert ad.vsl_started == 1
    assert ad.vsl_completed == 1
    assert ad.accounts == 1
    assert ad.subscriptions == 1
    assert ad.revenue_brl == 147.0

    other = ads["9004"]
    assert other.quiz_visitors == 0
    assert other.quiz_started == 1
    assert other.quiz_completed == 0
    assert other.spend_brl is None

    assert set(ads) == {"9003", "9004"}


def test_lead_recovery_exposes_whatsapp_connection_progress() -> None:
    started = UUID(int=201)
    failed = UUID(int=202)
    connected = UUID(int=203)
    account_only = UUID(int=204)
    blocked = UUID(int=205)

    contacts = [
        LeadContactRow(
            business_id=business_id,
            lead_name="Lead de teste",
            whatsapp_e164="+5500000000000",
            business_name="Empresa de teste",
            created_at=NOW - timedelta(minutes=30),
        )
        for business_id in [started, failed, connected, account_only, blocked]
    ]

    events = [
        event("whatsapp_connection_started", minutes=20, business_id=started),
        event("whatsapp_connection_started", minutes=19, business_id=failed),
        event("whatsapp_connection_failed", minutes=10, business_id=failed),
        event("activation_paywall_viewed", minutes=5, business_id=failed),
        event("whatsapp_connection_started", minutes=18, business_id=connected),
        event("whatsapp_connection_completed", minutes=7, business_id=connected),
        event("whatsapp_connection_started", minutes=17, business_id=blocked),
        event("whatsapp_connection_blocked", minutes=6, business_id=blocked),
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={},
        activation_at_by_business={},
        connection_at_by_business={
            connected: NOW - timedelta(minutes=7)
        },
        lead_contacts=contacts,
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    leads = {lead.business_id: lead for lead in summary.lead_recovery}

    assert leads[started].whatsapp_connection_last_event == "whatsapp_connection_started"
    assert leads[started].whatsapp_connection_started_at == NOW - timedelta(minutes=20)

    assert leads[failed].whatsapp_connection_last_event == "whatsapp_connection_failed"
    assert leads[failed].whatsapp_connection_last_event_at == NOW - timedelta(minutes=10)
    assert leads[failed].stage_key == "offer"

    assert leads[blocked].whatsapp_connection_last_event == "whatsapp_connection_blocked"

    assert leads[connected].whatsapp_connected_at == NOW - timedelta(minutes=7)
    assert leads[connected].stage_key == "whatsapp_connected_unpaid"
    assert leads[connected].activated is False

    assert leads[account_only].whatsapp_connection_last_event is None
    assert leads[account_only].whatsapp_connection_started_at is None
    assert leads[account_only].whatsapp_connected_at is None
    assert leads[account_only].stage_key == "account"

    assert all(lead.needs_follow_up for lead in leads.values())


def test_traffic_funnels_separate_acquisition_sources() -> None:
    organic = {"utm_source": "instagram", "utm_medium": "organic_social"}
    unknown_paid = {**acquisition(), "ad_id": "9999"}
    organic_business = UUID(int=301)

    events = base_events() + [
        event("quiz_started", minutes=18, properties=acquisition()),
        event("quiz_viewed", minutes=20, properties=organic),
        event("quiz_viewed", minutes=19, properties=organic),
        event("quiz_started", minutes=18, properties=organic),
        event("quiz_completed", minutes=17, properties=organic),
        event("signup_completed", minutes=12, properties=organic,
              business_id=organic_business),
        event("demo_page_view", minutes=11, business_id=organic_business),
        event("quiz_viewed", minutes=10, properties=unknown_paid),
        event("quiz_viewed", minutes=9),
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={
            BUSINESS: acquisition(),
            organic_business: organic,
        },
        activation_at_by_business={BUSINESS: NOW - timedelta(minutes=1)},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    traffic = {row.key: row for row in summary.traffic_funnels}

    assert traffic["meta_paid"].quiz_views == 1
    assert traffic["meta_paid"].quiz_started == 1
    assert traffic["meta_paid"].quiz_completed == 1
    assert traffic["meta_paid"].vsl_started == 1
    assert traffic["meta_paid"].vsl_completed == 1
    assert traffic["meta_paid"].accounts == 1
    assert traffic["meta_paid"].demo_entries == 1
    assert traffic["meta_paid"].checkout_started == 1
    assert traffic["meta_paid"].subscriptions == 1
    assert traffic["meta_paid"].activations == 1

    assert traffic["organic"].quiz_views == 1
    assert traffic["organic"].quiz_started == 1
    assert traffic["organic"].quiz_completed == 1
    assert traffic["organic"].accounts == 1
    assert traffic["organic"].demo_entries == 1
    assert traffic["organic"].subscriptions == 0

    assert traffic["paid_unverified"].quiz_views == 1
    assert traffic["unattributed"].quiz_views == 1
    assert traffic["unattributed"].accounts == 0

    assert metric(summary, "net_revenue").value == 147.0
    assert metric(summary, "subscriptions").value == 1
    assert metric(summary, "accounts").value == 2


def test_traffic_funnels_preserve_unverified_paid_traffic() -> None:
    events = base_events() + [
        event("quiz_viewed", minutes=8, properties={
            "utm_source": "fb",
        }),
        event("quiz_viewed", minutes=7, properties={
            "utm_source": "instagram",
            "utm_medium": "organic_social",
        }),
        event("quiz_viewed", minutes=6, properties={
            "ad_id": "9999",
            "utm_medium": "organic_social",
        }),
    ]

    for status in ("awaiting_integration", "ready"):
        summary = build_platform_analytics_summary(
            events=events,
            paid_checkouts=[paid_checkout()],
            refunds=[],
            active_subscriptions=[],
            acquisition_by_business={BUSINESS: acquisition()},
            activation_at_by_business={},
            meta_report=MetaAdsReport(
                status=status,
                note=None,
                ads=[],
            ),
            now=NOW,
            days=7,
        )

        traffic = {row.key: row for row in summary.traffic_funnels}

        assert traffic["meta_paid"].quiz_views == 0
        assert traffic["paid_unverified"].quiz_views == 1
        assert traffic["paid_unverified"].accounts == 1
        assert traffic["paid_unverified"].subscriptions == 1
        assert traffic["organic"].quiz_views == 1
        assert traffic["organic"].subscriptions == 0
        assert traffic["unattributed"].quiz_views == 1

        assert sum(row.subscriptions for row in traffic.values()) == 1
        assert metric(summary, "net_revenue").value == 147.0


def test_main_funnel_does_not_mix_paid_and_all_traffic() -> None:
    from dataclasses import replace

    organic = {"utm_source": "instagram", "utm_medium": "organic_social"}
    organic_visitor = UUID(int=401)
    organic_business = UUID(int=402)

    organic_events = [
        replace(
            event(name, minutes=minutes, properties=organic,
                  business_id=business_id),
            visitor_id=organic_visitor,
        )
        for name, minutes, business_id in [
            ("quiz_viewed", 20, None),
            ("quiz_completed", 17, None),
            ("quiz_demo_started", 15, None),
            ("signup_completed", 12, organic_business),
        ]
    ]

    summary = build_platform_analytics_summary(
        events=base_events() + organic_events,
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={
            BUSINESS: acquisition(),
            organic_business: organic,
        },
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}

    assert funnel["quiz_views"].value == 1
    assert funnel["quiz_views"].conversion_rate == 2.0

    assert funnel["quiz_views_all"].value == 2
    assert funnel["quiz_views_all"].conversion_rate is None

    assert funnel["quiz_completed"].value == 2
    assert funnel["quiz_completed"].conversion_rate == 100.0
    assert funnel["vsl_started"].conversion_rate == 100.0
    assert funnel["accounts"].value == 2
    assert funnel["accounts"].conversion_rate == 100.0

    assert metric(summary, "quiz_completion_rate").value == 100.0
    assert metric(summary, "signup_conversion_rate").value == 100.0
    assert metric(summary, "net_revenue").value == 147.0
    assert metric(summary, "subscriptions").value == 1


def test_unlinked_visitors_do_not_produce_false_conversion_rates() -> None:
    from dataclasses import replace

    new_visitor = UUID(int=403)

    events = [
        replace(row, visitor_id=new_visitor)
        if row.event_name != "quiz_viewed"
        else row
        for row in base_events()
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[paid_checkout()],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}

    assert funnel["quiz_views"].value == 1
    assert funnel["quiz_completed"].value == 1
    assert funnel["accounts"].value == 1

    assert funnel["quiz_completed"].conversion_rate is None
    assert metric(summary, "quiz_completion_rate").value is None
    assert metric(summary, "signup_conversion_rate").value is None

    assert funnel["vsl_started"].conversion_rate == 100.0
    assert funnel["accounts"].conversion_rate == 100.0
    assert metric(summary, "net_revenue").value == 147.0


def test_paid_media_financial_metrics_exclude_other_sources() -> None:
    from dataclasses import replace

    organic_business = UUID(int=501)
    unverified_business = UUID(int=502)
    existing_business = UUID(int=503)

    organic = {
        "utm_source": "instagram",
        "utm_medium": "organic_social",
    }
    unverified = {**acquisition(), "ad_id": "9999"}

    events = base_events() + [
        event("signup_completed", minutes=12,
              properties=organic, business_id=organic_business),
        event("signup_completed", minutes=11,
              properties=unverified, business_id=unverified_business),
    ]

    duplicate_payment = replace(
        paid_checkout(), checkout_id=UUID(int=511),
    )
    organic_payment = replace(
        paid_checkout(), checkout_id=UUID(int=512),
        business_id=organic_business,
    )
    unverified_payment = replace(
        paid_checkout(), checkout_id=UUID(int=513),
        business_id=unverified_business,
    )
    existing_payment = replace(
        paid_checkout(), checkout_id=UUID(int=514),
        business_id=existing_business,
    )

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[
            paid_checkout(),
            duplicate_payment,
            organic_payment,
            unverified_payment,
            existing_payment,
        ],
        refunds=[
            RefundRow(
                checkout_id=duplicate_payment.checkout_id,
                business_id=BUSINESS,
                amount_cents=4700,
                refunded_at=NOW - timedelta(minutes=2),
            ),
            RefundRow(
                checkout_id=organic_payment.checkout_id,
                business_id=organic_business,
                amount_cents=2000,
                refunded_at=NOW - timedelta(minutes=1),
            ),
        ],
        active_subscriptions=[],
        acquisition_by_business={
            BUSINESS: acquisition(),
            organic_business: organic,
            unverified_business: unverified,
            existing_business: acquisition(),
        },
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    assert metric(summary, "subscriptions").value == 5
    assert metric(summary, "revenue_received").value == 735.0
    assert metric(summary, "refunds").value == 67.0
    assert metric(summary, "net_revenue").value == 668.0

    assert metric(summary, "cac").value == 70.0
    assert metric(summary, "roas").value == 5.63

    ads = {item.ad_id: item for item in summary.ad_performance}
    assert ads["9003"].subscriptions == 3
    assert ads["9003"].revenue_brl == 394.0
    assert ads["9003"].cac_brl == 70.0
    assert ads["9003"].roas == 5.63

    assert ads["9999"].subscriptions == 1
    assert ads["9999"].revenue_brl == 147.0
    assert ads["9999"].cac_brl is None
    assert ads["9999"].roas is None


def test_ad_conversion_requires_linked_visitor_identity() -> None:
    from dataclasses import replace

    events = [
        replace(row, visitor_id=UUID(int=520))
        if row.event_name == "signup_completed"
        else row
        for row in base_events()
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={},
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )

    ad = next(
        item for item in summary.ad_performance
        if item.ad_id == "9003"
    )

    assert ad.quiz_visitors == 1
    assert ad.accounts == 1
    assert ad.account_conversion_rate is None


def test_activation_progress_counts_new_companies_by_stage() -> None:
    from dataclasses import replace

    failed = UUID(int=601)
    event_only = UUID(int=602)
    account_only = UUID(int=603)
    old = UUID(int=604)

    events = base_events() + [
        event("signup_completed", minutes=12, business_id=business_id)
        for business_id in (failed, event_only, account_only)
    ] + [
        event(name, minutes=minutes, business_id=business_id)
        for name, minutes, business_id in [
            ("demo_interaction", 8, BUSINESS),
            ("demo_interaction", 7, BUSINESS),
            ("whatsapp_connection_started", 8, BUSINESS),
            ("whatsapp_connection_started", 8, failed),
            ("whatsapp_connection_failed", 7, failed),
            ("whatsapp_connection_blocked", 6, failed),
            ("whatsapp_connection_started", 5, failed),
            ("whatsapp_connection_completed", 4, event_only),
            ("plan_selected", 8, BUSINESS),
            ("checkout_created", 8, BUSINESS),
            ("payment_submitted", 7, BUSINESS),
            ("payment_failed", 6, failed),
            ("demo_page_view", 7, old),
            ("whatsapp_connection_started", 6, old),
            ("whatsapp_connection_started", 5, None),
        ]
    ]
    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=[
            paid_checkout(),
            replace(paid_checkout(), checkout_id=UUID(int=611)),
            replace(paid_checkout(), checkout_id=UUID(int=612), business_id=old),
        ],
        refunds=[],
        active_subscriptions=[],
        acquisition_by_business={BUSINESS: acquisition()},
        activation_at_by_business={
            BUSINESS: NOW - timedelta(minutes=1),
            old: NOW - timedelta(minutes=1),
        },
        connection_at_by_business={
            BUSINESS: NOW - timedelta(minutes=1),
            old: NOW - timedelta(minutes=1),
        },
        meta_report=meta_report(),
        now=NOW,
        days=7,
    )
    progress = summary.activation_progress
    assert progress.accounts == 4
    assert progress.demo_entries == 1
    assert progress.demo_interactions == 1
    assert progress.connection_attempted == 2
    assert progress.connection_failed == 1
    assert progress.connection_blocked == 1
    assert progress.connection_completed_event == 2
    assert progress.connection_confirmed == 1
    assert progress.plan_views == 1
    assert progress.plan_selected == 1
    assert progress.checkout_started == 1
    assert progress.checkout_created == 1
    assert progress.payment_submitted == 1
    assert progress.payment_failed == 1
    assert progress.paid_businesses == 1
    assert progress.commercially_activated == 1
    assert metric(summary, "subscriptions").value == 3
    assert metric(summary, "net_revenue").value == 441.0

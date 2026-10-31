from datetime import UTC, datetime, timedelta

from app.platform_admin.analytics import (
    ActiveSubscriptionRow,
    AnalyticsEventRow,
    PaidCheckoutRow,
    build_platform_analytics_summary,
)


def event(name: str, when: datetime, **properties):
    return AnalyticsEventRow(
        event_name=name,
        occurred_at=when,
        properties=properties or None,
    )


def test_platform_analytics_uses_only_real_internal_metrics_and_marks_ad_metrics_future():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    events = [
        event("page_view", now - timedelta(hours=5), route_group="landing"),
        event("page_view", now - timedelta(hours=4), route_group="landing"),
        event("landing_vsl_started", now - timedelta(hours=4)),
        event("landing_cta_click", now - timedelta(hours=3)),
        event("signup_completed", now - timedelta(hours=3)),
        event("whatsapp_connection_completed", now - timedelta(hours=2)),
        event("payment_confirmed", now - timedelta(hours=1), placement="Instagram_Stories"),
    ]
    paid = [
        PaidCheckoutRow(
            plan_code="basic",
            billing_cycle="monthly",
            payment_method="pix",
            amount_cents=19_700,
            paid_at=now - timedelta(hours=1),
        )
    ]
    active = [
        ActiveSubscriptionRow(
            billing_cycle="monthly",
            amount_cents=19_700,
            status="active",
            access_until=now + timedelta(days=20),
        )
    ]

    summary = build_platform_analytics_summary(
        events=events,
        paid_checkouts=paid,
        active_subscriptions=active,
        now=now,
        days=7,
    )

    funnel = {item.key: item for item in summary.funnel}
    assert funnel["ad_clicks"].value is None
    assert funnel["ad_clicks"].status == "awaiting_integration"
    assert funnel["lpv"].value == 2
    assert funnel["vsl"].value == 1
    assert funnel["vsl"].conversion_rate == 50.0
    assert funnel["accounts"].value == 1
    assert funnel["activations"].value == 1
    assert funnel["subscriptions"].value == 1

    kpis = {item.key: item for item in summary.kpis}
    assert kpis["revenue_received"].value == 197.0
    assert kpis["mrr"].value == 197.0
    assert kpis["cta_clicks"].value == 1

    future = {item.key: item for item in summary.future_metrics}
    assert future["cac"].value is None
    assert future["cac"].status == "awaiting_integration"
    assert future["roas"].value is None
    assert future["trial_to_paid"].status == "structuring"
    assert future["ltv"].status == "awaiting_history"

    assert summary.subscriptions_by_plan.items[0].label == "Basic"
    assert summary.subscriptions_by_payment_method.items[0].label == "Pix"
    assert summary.subscriptions_by_placement.items[0].label == "Instagram_Stories"


def test_platform_analytics_normalizes_mrr_and_keeps_empty_breakdowns_honest():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    active = [
        ActiveSubscriptionRow(
            billing_cycle="quarterly",
            amount_cents=59_100,
            status="active",
            access_until=now + timedelta(days=50),
        ),
        ActiveSubscriptionRow(
            billing_cycle="annual",
            amount_cents=236_400,
            status="active",
            access_until=now + timedelta(days=200),
        ),
        ActiveSubscriptionRow(
            billing_cycle="monthly",
            amount_cents=19_700,
            status="canceled",
            access_until=now + timedelta(days=10),
        ),
    ]

    summary = build_platform_analytics_summary(
        events=[],
        paid_checkouts=[],
        active_subscriptions=active,
        now=now,
        days=30,
    )

    kpis = {item.key: item for item in summary.kpis}
    assert kpis["mrr"].value == 394.0
    assert summary.subscriptions_by_plan.status == "insufficient_data"
    assert summary.subscriptions_by_payment_method.status == "insufficient_data"
    assert summary.subscriptions_by_placement.status == "awaiting_integration"
    assert len(summary.series) == 31

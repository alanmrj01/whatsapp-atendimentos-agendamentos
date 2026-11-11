from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.meta_ads import MetaAdsReport, fetch_meta_ads_report
from app.core.config import Settings
from app.models import BillingCheckout, CommercialSubscription, MarketingFunnelEvent
from app.models.billing import billing_provider_environment
from app.platform_admin.schemas import (
    PlatformAnalyticsAdPerformanceItem,
    PlatformAnalyticsBreakdown,
    PlatformAnalyticsBreakdownItem,
    PlatformAnalyticsConversionBreakdown,
    PlatformAnalyticsConversionBreakdownItem,
    PlatformAnalyticsFunnelStep,
    PlatformAnalyticsKpi,
    PlatformAnalyticsQuizAnswerItem,
    PlatformAnalyticsQuizQuestion,
    PlatformAnalyticsSeriesPoint,
    PlatformAnalyticsSummaryResponse,
)

ANALYTICS_TZ = ZoneInfo("America/Sao_Paulo")
RELEVANT_MARKETING_EVENTS = {
    "page_view",
    "quiz_viewed",
    "quiz_started",
    "quiz_question_viewed",
    "quiz_answered",
    "quiz_completed",
    "quiz_result_viewed",
    "quiz_demo_started",
    "quiz_demo_progress",
    "quiz_demo_completed",
    "quiz_signup_cta_clicked",
    "signup_started",
    "signup_submit",
    "signup_completed",
    "preactivation_started",
    "demo_page_view",
    "demo_interaction",
    "activation_paywall_viewed",
    "plan_selected",
    "checkout_started",
    "checkout_created",
    "payment_submitted",
    "payment_confirmed",
    "payment_failed",
    "whatsapp_connection_started",
    "whatsapp_connection_blocked",
    "whatsapp_connection_failed",
    "whatsapp_connection_completed",
}
ACQUISITION_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_id",
    "campaign_id",
    "adset_id",
    "ad_id",
    "placement",
    "angle",
    "quiz_profile",
)
QUIZ_QUESTION_LABELS = {
    "q1": "Quem atende enquanto você trabalha?",
    "q2": "O que acontece nos dias corridos?",
    "q3": "O que você faz quando o cliente chama?",
    "q4": "Quanto o atendimento depende de você?",
    "q5": "O que mais incomoda no fim do dia?",
}


@dataclass(frozen=True)
class AnalyticsEventRow:
    visitor_id: UUID
    business_id: UUID | None
    user_id: UUID | None
    event_name: str
    occurred_at: datetime
    properties: dict[str, Any] | None


@dataclass(frozen=True)
class PaidCheckoutRow:
    checkout_id: UUID
    business_id: UUID
    plan_code: str
    billing_cycle: str
    payment_method: str
    amount_cents: int
    paid_at: datetime
    refund_status: str | None
    refunded_at: datetime | None


@dataclass(frozen=True)
class RefundRow:
    checkout_id: UUID
    business_id: UUID
    amount_cents: int
    refunded_at: datetime


@dataclass(frozen=True)
class ActiveSubscriptionRow:
    billing_cycle: str
    amount_cents: int
    status: str
    access_until: datetime


def _local_day(value: datetime) -> date:
    return value.astimezone(ANALYTICS_TZ).date()


def _ratio(current: int | float | None, previous: int | float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return round((current / previous) * 100, 1)


def _money_ratio(value: float, divisor: float) -> float | None:
    if divisor <= 0:
        return None
    return round(value / divisor, 2)


def _plan_label(value: str) -> str:
    return {"basic": "Basic", "plus": "Plus"}.get(value, value.title())


def _payment_label(value: str) -> str:
    return {
        "pix": "Pix",
        "pix_automatic": "Pix Automático",
        "credit_card": "Cartão",
    }.get(value, value.replace("_", " ").title())


def _property(event: AnalyticsEventRow, key: str) -> str | None:
    value = (event.properties or {}).get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _business_ids(events: list[AnalyticsEventRow], event_name: str) -> set[UUID]:
    return {
        event.business_id
        for event in events
        if event.event_name == event_name and event.business_id is not None
    }


def _visitor_ids(events: list[AnalyticsEventRow], event_name: str) -> set[UUID]:
    return {event.visitor_id for event in events if event.event_name == event_name}


def _demo_business_ids(events: list[AnalyticsEventRow]) -> set[UUID]:
    primary = _business_ids(events, "preactivation_started")
    fallback = _business_ids(events, "demo_page_view")
    return primary | fallback


def _mrr_cents(subscriptions: list[ActiveSubscriptionRow], now: datetime) -> int:
    divisors = {"monthly": 1, "quarterly": 3, "annual": 12}
    total = 0.0
    for subscription in subscriptions:
        if subscription.status != "active" or subscription.access_until <= now:
            continue
        divisor = divisors.get(subscription.billing_cycle)
        if divisor:
            total += subscription.amount_cents / divisor
    return int(round(total))


def _clean_acquisition(properties: dict[str, Any] | None) -> dict[str, str]:
    if not properties:
        return {}
    result: dict[str, str] = {}
    for key in ACQUISITION_KEYS:
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:256]
    return result


def _signup_acquisition_from_events(
    events: list[AnalyticsEventRow],
) -> dict[UUID, dict[str, str]]:
    result: dict[UUID, dict[str, str]] = {}
    for event in sorted(events, key=lambda item: item.occurred_at):
        if event.event_name != "signup_completed" or event.business_id is None:
            continue
        result.setdefault(event.business_id, _clean_acquisition(event.properties))
    return result


def _breakdown(
    *,
    key: str,
    label: str,
    counts: Counter[str],
    empty_note: str,
) -> PlatformAnalyticsBreakdown:
    return PlatformAnalyticsBreakdown(
        key=key,
        label=label,
        status="ready" if counts else "insufficient_data",
        note=None if counts else empty_note,
        items=[
            PlatformAnalyticsBreakdownItem(key=item, label=item, value=value)
            for item, value in counts.most_common()
        ],
    )


def _conversion_breakdown(
    *,
    key: str,
    label: str,
    dimension: str,
    base_events: list[AnalyticsEventRow],
    signup_events: list[AnalyticsEventRow],
    paid_checkouts: list[PaidCheckoutRow],
    activation_businesses: set[UUID],
    acquisition_by_business: dict[UUID, dict[str, str]],
) -> PlatformAnalyticsConversionBreakdown:
    base_visitors: dict[str, set[UUID]] = defaultdict(set)
    accounts: dict[str, set[UUID]] = defaultdict(set)
    subscriptions: Counter[str] = Counter()
    activations: dict[str, set[UUID]] = defaultdict(set)
    revenue: Counter[str] = Counter()

    for event in base_events:
        value = _property(event, dimension)
        if value:
            base_visitors[value].add(event.visitor_id)

    for event in signup_events:
        value = _property(event, dimension)
        if value and event.business_id is not None:
            accounts[value].add(event.business_id)

    for checkout in paid_checkouts:
        value = acquisition_by_business.get(checkout.business_id, {}).get(dimension)
        if not value:
            continue
        subscriptions[value] += 1
        net_amount = 0 if checkout.refund_status == "refunded" else checkout.amount_cents
        revenue[value] += net_amount

    for business_id in activation_businesses:
        value = acquisition_by_business.get(business_id, {}).get(dimension)
        if value:
            activations[value].add(business_id)

    values = set(base_visitors) | set(accounts) | set(subscriptions) | set(activations)
    items = [
        PlatformAnalyticsConversionBreakdownItem(
            key=value,
            label=value.replace("_", " ").title(),
            quiz_visitors=len(base_visitors[value]),
            accounts=len(accounts[value]),
            subscriptions=subscriptions[value],
            activations=len(activations[value]),
            revenue_brl=round(revenue[value] / 100, 2),
        )
        for value in values
    ]
    items.sort(
        key=lambda item: (
            item.subscriptions,
            item.accounts,
            item.quiz_visitors,
        ),
        reverse=True,
    )
    return PlatformAnalyticsConversionBreakdown(
        key=key,
        label=label,
        status="ready" if items else "insufficient_data",
        note=None if items else "Ainda não há dados atribuídos suficientes no período.",
        items=items,
    )


def _quiz_questions(
    *,
    events: list[AnalyticsEventRow],
    paid_businesses: set[UUID],
) -> list[PlatformAnalyticsQuizQuestion]:
    latest_answers: dict[tuple[UUID, str], AnalyticsEventRow] = {}
    signup_visitors = _visitor_ids(events, "signup_completed")
    paid_visitors = {
        event.visitor_id
        for event in events
        if event.event_name == "signup_completed"
        and event.business_id in paid_businesses
    }

    for event in events:
        if event.event_name != "quiz_answered":
            continue
        question_id = _property(event, "question_id")
        answer_id = _property(event, "answer_id")
        if not question_id or not answer_id:
            continue
        lookup = (event.visitor_id, question_id)
        previous = latest_answers.get(lookup)
        if previous is None or event.occurred_at >= previous.occurred_at:
            latest_answers[lookup] = event

    grouped: dict[str, dict[str, set[UUID]]] = defaultdict(lambda: defaultdict(set))
    profiles: dict[tuple[str, str], str] = {}
    for (visitor_id, question_id), event in latest_answers.items():
        answer_id = _property(event, "answer_id")
        if not answer_id:
            continue
        grouped[question_id][answer_id].add(visitor_id)
        profiles[(question_id, answer_id)] = (
            _property(event, "profile_dimension") or "unknown"
        )

    questions: list[PlatformAnalyticsQuizQuestion] = []
    for question_id in sorted(grouped):
        answer_sets = grouped[question_id]
        total = sum(len(visitors) for visitors in answer_sets.values())
        answers: list[PlatformAnalyticsQuizAnswerItem] = []
        for answer_id in sorted(answer_sets):
            visitors = answer_sets[answer_id]
            accounts = len(visitors & signup_visitors)
            subscriptions = len(visitors & paid_visitors)
            answers.append(
                PlatformAnalyticsQuizAnswerItem(
                    answer_id=answer_id,
                    profile_dimension=profiles[(question_id, answer_id)],
                    responses=len(visitors),
                    share=_ratio(len(visitors), total),
                    accounts=accounts,
                    account_conversion_rate=_ratio(accounts, len(visitors)),
                    subscriptions=subscriptions,
                    purchase_conversion_rate=_ratio(subscriptions, len(visitors)),
                )
            )
        questions.append(
            PlatformAnalyticsQuizQuestion(
                question_id=question_id,
                label=QUIZ_QUESTION_LABELS.get(question_id, question_id.upper()),
                responses=total,
                answers=answers,
            )
        )
    return questions


def _ad_performance(
    *,
    events: list[AnalyticsEventRow],
    paid_checkouts: list[PaidCheckoutRow],
    activation_businesses: set[UUID],
    acquisition_by_business: dict[UUID, dict[str, str]],
    meta_report: MetaAdsReport,
) -> list[PlatformAnalyticsAdPerformanceItem]:
    meta_by_ad = {item.ad_id: item for item in meta_report.ads}
    quiz_visitors: dict[str, set[UUID]] = defaultdict(set)
    quiz_completed: dict[str, set[UUID]] = defaultdict(set)
    accounts: dict[str, set[UUID]] = defaultdict(set)
    subscriptions: Counter[str] = Counter()
    activations: dict[str, set[UUID]] = defaultdict(set)
    revenue_cents: Counter[str] = Counter()
    event_names: dict[str, dict[str, str]] = {}

    for event in events:
        ad_id = _property(event, "ad_id")
        if not ad_id:
            continue
        event_names.setdefault(ad_id, _clean_acquisition(event.properties))
        if event.event_name == "quiz_viewed":
            quiz_visitors[ad_id].add(event.visitor_id)
        elif event.event_name == "quiz_completed":
            quiz_completed[ad_id].add(event.visitor_id)
        elif event.event_name == "signup_completed" and event.business_id is not None:
            accounts[ad_id].add(event.business_id)

    for checkout in paid_checkouts:
        acquisition = acquisition_by_business.get(checkout.business_id, {})
        ad_id = acquisition.get("ad_id")
        if not ad_id:
            continue
        event_names.setdefault(ad_id, acquisition)
        subscriptions[ad_id] += 1
        if checkout.refund_status != "refunded":
            revenue_cents[ad_id] += checkout.amount_cents

    for business_id in activation_businesses:
        acquisition = acquisition_by_business.get(business_id, {})
        ad_id = acquisition.get("ad_id")
        if ad_id:
            activations[ad_id].add(business_id)
            event_names.setdefault(ad_id, acquisition)

    ad_ids = (
        set(meta_by_ad)
        | set(quiz_visitors)
        | set(quiz_completed)
        | set(accounts)
        | set(subscriptions)
        | set(activations)
    )
    rows: list[PlatformAnalyticsAdPerformanceItem] = []
    for ad_id in ad_ids:
        meta = meta_by_ad.get(ad_id)
        acquisition = event_names.get(ad_id, {})
        spend = meta.spend_brl if meta is not None else None
        subscriptions_count = subscriptions[ad_id]
        revenue_brl = round(revenue_cents[ad_id] / 100, 2)
        rows.append(
            PlatformAnalyticsAdPerformanceItem(
                campaign_id=meta.campaign_id if meta else acquisition.get("campaign_id"),
                campaign_name=meta.campaign_name if meta else acquisition.get("utm_campaign"),
                adset_id=meta.adset_id if meta else acquisition.get("adset_id"),
                adset_name=meta.adset_name if meta else acquisition.get("utm_term"),
                ad_id=ad_id,
                ad_name=meta.ad_name if meta else acquisition.get("utm_content", ad_id),
                spend_brl=round(spend, 2) if spend is not None else None,
                impressions=meta.impressions if meta else None,
                reach=meta.reach if meta else None,
                link_clicks=meta.link_clicks if meta else None,
                ctr=round(meta.ctr, 2) if meta and meta.ctr is not None else None,
                cpc=round(meta.cpc, 2) if meta and meta.cpc is not None else None,
                cpm=round(meta.cpm, 2) if meta and meta.cpm is not None else None,
                quiz_visitors=len(quiz_visitors[ad_id]),
                quiz_completed=len(quiz_completed[ad_id]),
                accounts=len(accounts[ad_id]),
                subscriptions=subscriptions_count,
                activations=len(activations[ad_id]),
                revenue_brl=revenue_brl,
                account_conversion_rate=_ratio(
                    len(accounts[ad_id]), len(quiz_visitors[ad_id])
                ),
                cac_brl=(
                    round(spend / subscriptions_count, 2)
                    if spend is not None and subscriptions_count > 0
                    else None
                ),
                roas=(
                    round(revenue_brl / spend, 2)
                    if spend is not None and spend > 0
                    else None
                ),
            )
        )
    rows.sort(
        key=lambda item: (
            item.subscriptions,
            item.accounts,
            item.quiz_visitors,
            item.spend_brl or 0,
        ),
        reverse=True,
    )
    return rows


def build_platform_analytics_summary(
    *,
    events: list[AnalyticsEventRow],
    paid_checkouts: list[PaidCheckoutRow],
    refunds: list[RefundRow],
    active_subscriptions: list[ActiveSubscriptionRow],
    acquisition_by_business: dict[UUID, dict[str, str]],
    meta_report: MetaAdsReport,
    now: datetime,
    days: int,
) -> PlatformAnalyticsSummaryResponse:
    period_start = now - timedelta(days=days)

    quiz_visitors = _visitor_ids(events, "quiz_viewed")
    quiz_completed_visitors = _visitor_ids(events, "quiz_completed")
    vsl_started_visitors = _visitor_ids(events, "quiz_demo_started")
    vsl_completed_visitors = _visitor_ids(events, "quiz_demo_completed")
    signup_cta_visitors = _visitor_ids(events, "quiz_signup_cta_clicked")
    account_businesses = _business_ids(events, "signup_completed")
    demo_businesses = _demo_business_ids(events)
    plan_businesses = _business_ids(events, "activation_paywall_viewed")
    checkout_businesses = _business_ids(events, "checkout_started")
    activation_businesses = _business_ids(events, "whatsapp_connection_completed")

    sales = len(paid_checkouts)
    paid_businesses = {checkout.business_id for checkout in paid_checkouts}
    revenue_cents = sum(checkout.amount_cents for checkout in paid_checkouts)
    refund_cents = sum(refund.amount_cents for refund in refunds)
    net_revenue_cents = revenue_cents - refund_cents
    mrr_cents = _mrr_cents(active_subscriptions, now)

    meta_spend = sum(item.spend_brl for item in meta_report.ads)
    meta_impressions = sum(item.impressions for item in meta_report.ads)
    meta_link_clicks = sum(item.link_clicks for item in meta_report.ads)
    meta_ready = meta_report.status == "ready"
    meta_ctr = _ratio(meta_link_clicks, meta_impressions) if meta_ready else None
    meta_cpc = _money_ratio(meta_spend, meta_link_clicks) if meta_ready else None
    meta_cpm = (
        round((meta_spend / meta_impressions) * 1000, 2)
        if meta_ready and meta_impressions > 0
        else None
    )
    cac = _money_ratio(meta_spend, sales) if meta_ready else None
    roas = (
        round((net_revenue_cents / 100) / meta_spend, 2)
        if meta_ready and meta_spend > 0
        else None
    )

    funnel = [
        PlatformAnalyticsFunnelStep(
            key="ad_clicks",
            label="Cliques",
            value=meta_link_clicks if meta_ready else None,
            status="ready" if meta_ready else "awaiting_integration",
            note="Cliques em links reportados pela Meta Ads.",
        ),
        PlatformAnalyticsFunnelStep(
            key="quiz_views",
            label="Quiz aberto",
            value=len(quiz_visitors),
            conversion_rate=_ratio(
                len(quiz_visitors),
                meta_link_clicks if meta_ready else None,
            ),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="quiz_completed",
            label="Quiz concluído",
            value=len(quiz_completed_visitors),
            conversion_rate=_ratio(len(quiz_completed_visitors), len(quiz_visitors)),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="vsl_started",
            label="VSL iniciada",
            value=len(vsl_started_visitors),
            conversion_rate=_ratio(
                len(vsl_started_visitors), len(quiz_completed_visitors)
            ),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="accounts",
            label="Conta criada",
            value=len(account_businesses),
            conversion_rate=_ratio(len(account_businesses), len(vsl_started_visitors)),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="demo_entries",
            label="Demonstração",
            value=len(demo_businesses),
            conversion_rate=_ratio(len(demo_businesses), len(account_businesses)),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="checkout_started",
            label="Checkout",
            value=len(checkout_businesses),
            conversion_rate=_ratio(len(checkout_businesses), len(demo_businesses)),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="subscriptions",
            label="Compra",
            value=sales,
            conversion_rate=_ratio(sales, len(checkout_businesses)),
            status="ready",
            note="Pagamentos confirmados no banco transacional.",
        ),
        PlatformAnalyticsFunnelStep(
            key="activations",
            label="Ativação",
            value=len(activation_businesses),
            conversion_rate=_ratio(len(activation_businesses), sales),
            status="ready",
            note="Conexão do WhatsApp concluída.",
        ),
    ]

    def meta_kpi(
        key: str,
        label: str,
        value: int | float | None,
        format: str,
        note: str,
    ) -> PlatformAnalyticsKpi:
        return PlatformAnalyticsKpi(
            key=key,
            label=label,
            value=value if meta_ready else None,
            format=format,  # type: ignore[arg-type]
            status="ready" if meta_ready else "awaiting_integration",
            note=note if meta_ready else (meta_report.note or note),
        )

    kpis = [
        meta_kpi(
            "ad_spend",
            "Gasto com anúncios",
            round(meta_spend, 2),
            "currency_brl",
            "Valor reportado pela Meta Ads para campanhas Alovia.",
        ),
        meta_kpi(
            "impressions",
            "Impressões",
            meta_impressions,
            "count",
            "Impressões reportadas pela Meta Ads.",
        ),
        meta_kpi(
            "link_clicks",
            "Cliques no link",
            meta_link_clicks,
            "count",
            "Cliques em links reportados pela Meta Ads.",
        ),
        meta_kpi(
            "ctr",
            "CTR",
            meta_ctr,
            "percent",
            "Click Through Rate / taxa de cliques em link.",
        ),
        meta_kpi(
            "cpc",
            "CPC",
            meta_cpc,
            "currency_brl",
            "Cost Per Click / custo por clique em link.",
        ),
        meta_kpi(
            "cpm",
            "CPM",
            meta_cpm,
            "currency_brl",
            "Cost Per Mille / custo por mil impressões.",
        ),
        meta_kpi(
            "cac",
            "CAC",
            cac,
            "currency_brl",
            "Customer Acquisition Cost / custo de aquisição por pagamento confirmado.",
        ),
        meta_kpi(
            "roas",
            "ROAS",
            roas,
            "ratio",
            "Return on Ad Spend / retorno sobre investimento em anúncios usando receita líquida.",
        ),
        PlatformAnalyticsKpi(
            key="net_revenue",
            label="Receita líquida",
            value=net_revenue_cents / 100,
            format="currency_brl",
            note="Receita recebida menos reembolsos confirmados no período.",
        ),
        PlatformAnalyticsKpi(
            key="revenue_received",
            label="Receita recebida",
            value=revenue_cents / 100,
            format="currency_brl",
            note="Pagamentos confirmados no período.",
        ),
        PlatformAnalyticsKpi(
            key="refunds",
            label="Reembolsos",
            value=refund_cents / 100,
            format="currency_brl",
            note="Reembolsos confirmados no período.",
        ),
        PlatformAnalyticsKpi(
            key="mrr",
            label="MRR",
            value=mrr_cents / 100,
            format="currency_brl",
            note="Monthly Recurring Revenue / receita recorrente mensal das assinaturas ativas.",
        ),
        PlatformAnalyticsKpi(
            key="subscriptions",
            label="Vendas",
            value=sales,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="quiz_views",
            label="Quiz aberto",
            value=len(quiz_visitors),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="quiz_completion_rate",
            label="Conclusão do quiz",
            value=_ratio(len(quiz_completed_visitors), len(quiz_visitors)),
            format="percent",
        ),
        PlatformAnalyticsKpi(
            key="vsl_started",
            label="VSL iniciadas",
            value=len(vsl_started_visitors),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="vsl_completion_rate",
            label="Conclusão da VSL",
            value=_ratio(len(vsl_completed_visitors), len(vsl_started_visitors)),
            format="percent",
        ),
        PlatformAnalyticsKpi(
            key="signup_cta",
            label="Cliques para criar conta",
            value=len(signup_cta_visitors),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="accounts",
            label="Contas criadas",
            value=len(account_businesses),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="signup_conversion_rate",
            label="Quiz → conta",
            value=_ratio(len(account_businesses), len(quiz_visitors)),
            format="percent",
        ),
        PlatformAnalyticsKpi(
            key="demo_entries",
            label="Demonstrações",
            value=len(demo_businesses),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="plan_views",
            label="Plano exibido",
            value=len(plan_businesses),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="checkout_started",
            label="Checkouts iniciados",
            value=len(checkout_businesses),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="trial_to_paid",
            label="Conta → pago",
            value=_ratio(sales, len(account_businesses)),
            format="percent",
            note="Conversão de conta gratuita para pagamento confirmado.",
        ),
        PlatformAnalyticsKpi(
            key="activations",
            label="Ativações",
            value=len(activation_businesses),
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="activation_rate",
            label="Compra → ativação",
            value=_ratio(len(activation_businesses), sales),
            format="percent",
        ),
    ]

    plan_counts = Counter(checkout.plan_code for checkout in paid_checkouts)
    payment_counts = Counter(checkout.payment_method for checkout in paid_checkouts)
    placement_counts = Counter(
        acquisition_by_business.get(checkout.business_id, {}).get("placement")
        for checkout in paid_checkouts
    )
    placement_counts.pop(None, None)

    subscriptions_by_plan = PlatformAnalyticsBreakdown(
        key="subscriptions_by_plan",
        label="Vendas por plano",
        status="ready" if plan_counts else "insufficient_data",
        note=None if plan_counts else "Ainda não há pagamentos confirmados no período.",
        items=[
            PlatformAnalyticsBreakdownItem(key=key, label=_plan_label(key), value=value)
            for key, value in plan_counts.most_common()
        ],
    )
    subscriptions_by_payment_method = PlatformAnalyticsBreakdown(
        key="subscriptions_by_payment_method",
        label="Vendas por pagamento",
        status="ready" if payment_counts else "insufficient_data",
        note=None if payment_counts else "Ainda não há pagamentos confirmados no período.",
        items=[
            PlatformAnalyticsBreakdownItem(
                key=key,
                label=_payment_label(key),
                value=value,
            )
            for key, value in payment_counts.most_common()
        ],
    )
    subscriptions_by_placement = PlatformAnalyticsBreakdown(
        key="subscriptions_by_placement",
        label="Vendas por posicionamento",
        status="ready" if placement_counts else "insufficient_data",
        note=None if placement_counts else "Aguardando vendas atribuídas por posicionamento.",
        items=[
            PlatformAnalyticsBreakdownItem(key=str(key), label=str(key), value=value)
            for key, value in placement_counts.most_common()
        ],
    )

    signup_events = [event for event in events if event.event_name == "signup_completed"]
    conversions_by_angle = _conversion_breakdown(
        key="conversions_by_angle",
        label="Desempenho por ângulo",
        dimension="angle",
        base_events=[event for event in events if event.event_name == "quiz_viewed"],
        signup_events=signup_events,
        paid_checkouts=paid_checkouts,
        activation_businesses=activation_businesses,
        acquisition_by_business=acquisition_by_business,
    )
    conversions_by_quiz_profile = _conversion_breakdown(
        key="conversions_by_quiz_profile",
        label="Desempenho por perfil do quiz",
        dimension="quiz_profile",
        base_events=[
            AnalyticsEventRow(
                visitor_id=event.visitor_id,
                business_id=event.business_id,
                user_id=event.user_id,
                event_name=event.event_name,
                occurred_at=event.occurred_at,
                properties={
                    **(event.properties or {}),
                    "quiz_profile": _property(event, "result_profile"),
                },
            )
            for event in events
            if event.event_name == "quiz_result_viewed"
            and _property(event, "result_profile")
        ],
        signup_events=signup_events,
        paid_checkouts=paid_checkouts,
        activation_businesses=activation_businesses,
        acquisition_by_business=acquisition_by_business,
    )

    issue_groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        if event.event_name not in {"whatsapp_connection_failed", "whatsapp_connection_blocked"}:
            continue
        reason = _property(event, "reason") or "unknown"
        kind = "Falha" if event.event_name == "whatsapp_connection_failed" else "Pendente"
        identity = str(event.business_id or event.visitor_id)
        issue_groups[(kind, reason)].add(identity)
    issue_counts = Counter(
        {f"{kind}: {reason}": len(identities) for (kind, reason), identities in issue_groups.items()}
    )
    onboarding_issues = _breakdown(
        key="onboarding_issues",
        label="Gargalos de ativação",
        counts=issue_counts,
        empty_note="Nenhuma falha ou bloqueio de conexão registrado no período.",
    )

    quiz_questions = _quiz_questions(events=events, paid_businesses=paid_businesses)
    ad_performance = _ad_performance(
        events=events,
        paid_checkouts=paid_checkouts,
        activation_businesses=activation_businesses,
        acquisition_by_business=acquisition_by_business,
        meta_report=meta_report,
    )

    local_end = _local_day(now)
    local_start = _local_day(period_start)
    day_count = (local_end - local_start).days
    series_sets: dict[date, dict[str, set[str]]] = {
        local_start + timedelta(days=offset): {
            "quiz_views": set(),
            "quiz_completed": set(),
            "vsl_started": set(),
            "vsl_completed": set(),
            "accounts": set(),
            "demo_entries": set(),
            "checkout_started": set(),
            "activations": set(),
        }
        for offset in range(day_count + 1)
    }
    for event in events:
        bucket = series_sets.get(_local_day(event.occurred_at))
        if bucket is None:
            continue
        visitor = str(event.visitor_id)
        business = str(event.business_id) if event.business_id is not None else visitor
        if event.event_name == "quiz_viewed":
            bucket["quiz_views"].add(visitor)
        elif event.event_name == "quiz_completed":
            bucket["quiz_completed"].add(visitor)
        elif event.event_name == "quiz_demo_started":
            bucket["vsl_started"].add(visitor)
        elif event.event_name == "quiz_demo_completed":
            bucket["vsl_completed"].add(visitor)
        elif event.event_name == "signup_completed":
            bucket["accounts"].add(business)
        elif event.event_name in {"preactivation_started", "demo_page_view"}:
            bucket["demo_entries"].add(business)
        elif event.event_name == "checkout_started":
            bucket["checkout_started"].add(business)
        elif event.event_name == "whatsapp_connection_completed":
            bucket["activations"].add(business)

    subscriptions_by_day: Counter[date] = Counter(
        _local_day(checkout.paid_at) for checkout in paid_checkouts
    )
    series = []
    for day, values in sorted(series_sets.items()):
        quiz_views_value = len(values["quiz_views"])
        vsl_started_value = len(values["vsl_started"])
        series.append(
            PlatformAnalyticsSeriesPoint(
                date=day,
                lpv=quiz_views_value,
                vsl=vsl_started_value,
                quiz_views=quiz_views_value,
                quiz_completed=len(values["quiz_completed"]),
                vsl_started=vsl_started_value,
                vsl_completed=len(values["vsl_completed"]),
                accounts=len(values["accounts"]),
                demo_entries=len(values["demo_entries"]),
                checkout_started=len(values["checkout_started"]),
                subscriptions=subscriptions_by_day[day],
                activations=len(values["activations"]),
            )
        )

    future_metrics = [
        PlatformAnalyticsKpi(
            key="roi",
            label="ROI",
            value=None,
            format="ratio",
            status="structuring",
            note="Return on Investment / retorno sobre investimento: depende dos demais custos operacionais.",
        ),
        PlatformAnalyticsKpi(
            key="margin",
            label="Margem",
            value=None,
            format="percent",
            status="structuring",
            note="Aguardando definição consolidada de custos além da mídia.",
        ),
        PlatformAnalyticsKpi(
            key="churn",
            label="Churn",
            value=None,
            format="percent",
            status="awaiting_history",
            note="Cancelamento: o cálculo ficará confiável quando houver base histórica suficiente.",
        ),
        PlatformAnalyticsKpi(
            key="ltv",
            label="LTV",
            value=None,
            format="currency_brl",
            status="awaiting_history",
            note="Lifetime Value / valor do cliente ao longo do tempo.",
        ),
        PlatformAnalyticsKpi(
            key="retention",
            label="Retenção",
            value=None,
            format="percent",
            status="awaiting_history",
            note="Aguardando coortes e histórico de renovação.",
        ),
    ]

    return PlatformAnalyticsSummaryResponse(
        generated_at=now,
        period_days=days,
        period_start=period_start,
        period_end=now,
        funnel=funnel,
        kpis=kpis,
        subscriptions_by_plan=subscriptions_by_plan,
        subscriptions_by_payment_method=subscriptions_by_payment_method,
        subscriptions_by_placement=subscriptions_by_placement,
        conversions_by_angle=conversions_by_angle,
        conversions_by_quiz_profile=conversions_by_quiz_profile,
        onboarding_issues=onboarding_issues,
        quiz_questions=quiz_questions,
        ad_performance=ad_performance,
        meta_ads_status="ready" if meta_ready else "awaiting_integration",
        meta_ads_note=meta_report.note,
        series=series,
        future_metrics=future_metrics,
    )


class PlatformAnalyticsService:
    def __init__(self, db: AsyncSession, settings: Settings):
        self.db = db
        self.settings = settings

    async def summary(self, days: int) -> PlatformAnalyticsSummaryResponse:
        now = datetime.now(UTC)
        period_start = now - timedelta(days=days)
        provider_environment = billing_provider_environment()

        event_rows = await self.db.execute(
            select(
                MarketingFunnelEvent.visitor_id,
                MarketingFunnelEvent.business_id,
                MarketingFunnelEvent.user_id,
                MarketingFunnelEvent.event_name,
                MarketingFunnelEvent.occurred_at,
                MarketingFunnelEvent.properties,
            ).where(
                MarketingFunnelEvent.occurred_at >= period_start,
                MarketingFunnelEvent.event_name.in_(RELEVANT_MARKETING_EVENTS),
            )
        )
        events = [
            AnalyticsEventRow(
                visitor_id=visitor_id,
                business_id=business_id,
                user_id=user_id,
                event_name=event_name,
                occurred_at=occurred_at,
                properties=properties,
            )
            for visitor_id, business_id, user_id, event_name, occurred_at, properties in event_rows
        ]

        paid_rows = await self.db.execute(
            select(
                BillingCheckout.id,
                BillingCheckout.business_id,
                BillingCheckout.plan_code,
                BillingCheckout.billing_cycle,
                BillingCheckout.payment_method,
                BillingCheckout.amount_cents,
                BillingCheckout.paid_at,
                CommercialSubscription.refund_status,
                CommercialSubscription.refunded_at,
            )
            .outerjoin(
                CommercialSubscription,
                CommercialSubscription.checkout_id == BillingCheckout.id,
            )
            .where(
                BillingCheckout.provider_environment == provider_environment,
                BillingCheckout.status == "paid",
                BillingCheckout.paid_at.is_not(None),
                BillingCheckout.paid_at >= period_start,
            )
        )
        paid_checkouts = [
            PaidCheckoutRow(
                checkout_id=checkout_id,
                business_id=business_id,
                plan_code=plan_code,
                billing_cycle=billing_cycle,
                payment_method=payment_method,
                amount_cents=amount_cents,
                paid_at=paid_at,
                refund_status=refund_status,
                refunded_at=refunded_at,
            )
            for (
                checkout_id,
                business_id,
                plan_code,
                billing_cycle,
                payment_method,
                amount_cents,
                paid_at,
                refund_status,
                refunded_at,
            ) in paid_rows
            if paid_at is not None
        ]

        refund_rows = await self.db.execute(
            select(
                BillingCheckout.id,
                BillingCheckout.business_id,
                BillingCheckout.amount_cents,
                CommercialSubscription.refunded_at,
            )
            .join(
                CommercialSubscription,
                CommercialSubscription.checkout_id == BillingCheckout.id,
            )
            .where(
                BillingCheckout.provider_environment == provider_environment,
                CommercialSubscription.provider_environment == provider_environment,
                CommercialSubscription.refund_status == "refunded",
                CommercialSubscription.refunded_at.is_not(None),
                CommercialSubscription.refunded_at >= period_start,
            )
        )
        refunds = [
            RefundRow(
                checkout_id=checkout_id,
                business_id=business_id,
                amount_cents=amount_cents,
                refunded_at=refunded_at,
            )
            for checkout_id, business_id, amount_cents, refunded_at in refund_rows
            if refunded_at is not None
        ]

        active_rows = await self.db.execute(
            select(
                CommercialSubscription.billing_cycle,
                BillingCheckout.amount_cents,
                CommercialSubscription.status,
                CommercialSubscription.access_until,
            )
            .join(
                BillingCheckout,
                BillingCheckout.id == CommercialSubscription.checkout_id,
            )
            .where(
                CommercialSubscription.provider_environment == provider_environment,
                BillingCheckout.provider_environment == provider_environment,
                CommercialSubscription.status == "active",
                CommercialSubscription.refund_status != "refunded",
                CommercialSubscription.access_until > now,
            )
        )
        active_subscriptions = [
            ActiveSubscriptionRow(
                billing_cycle=billing_cycle,
                amount_cents=amount_cents,
                status=status,
                access_until=access_until,
            )
            for billing_cycle, amount_cents, status, access_until in active_rows
        ]

        relevant_business_ids = {
            event.business_id for event in events if event.business_id is not None
        } | {checkout.business_id for checkout in paid_checkouts}
        acquisition_by_business = _signup_acquisition_from_events(events)
        missing_business_ids = relevant_business_ids - set(acquisition_by_business)
        if missing_business_ids:
            acquisition_rows = await self.db.execute(
                select(
                    MarketingFunnelEvent.business_id,
                    MarketingFunnelEvent.occurred_at,
                    MarketingFunnelEvent.properties,
                )
                .where(
                    MarketingFunnelEvent.business_id.in_(missing_business_ids),
                    MarketingFunnelEvent.event_name == "signup_completed",
                )
                .order_by(MarketingFunnelEvent.occurred_at.asc())
            )
            for business_id, _, properties in acquisition_rows:
                if business_id is not None:
                    acquisition_by_business.setdefault(
                        business_id,
                        _clean_acquisition(properties),
                    )

        meta_report = await fetch_meta_ads_report(
            self.settings,
            since=_local_day(period_start),
            until=_local_day(now),
        )

        return build_platform_analytics_summary(
            events=events,
            paid_checkouts=paid_checkouts,
            refunds=refunds,
            active_subscriptions=active_subscriptions,
            acquisition_by_business=acquisition_by_business,
            meta_report=meta_report,
            now=now,
            days=days,
        )

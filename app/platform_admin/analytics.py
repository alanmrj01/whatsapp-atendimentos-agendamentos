from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BillingCheckout, CommercialSubscription, MarketingFunnelEvent
from app.models.billing import billing_provider_environment
from app.platform_admin.schemas import (
    PlatformAnalyticsBreakdown,
    PlatformAnalyticsBreakdownItem,
    PlatformAnalyticsFunnelStep,
    PlatformAnalyticsKpi,
    PlatformAnalyticsSeriesPoint,
    PlatformAnalyticsSummaryResponse,
)

ANALYTICS_TZ = ZoneInfo("America/Sao_Paulo")
RELEVANT_MARKETING_EVENTS = {
    "page_view",
    "landing_vsl_started",
    "landing_cta_click",
    "signup_completed",
    "whatsapp_connection_completed",
    "payment_confirmed",
}


@dataclass(frozen=True)
class AnalyticsEventRow:
    event_name: str
    occurred_at: datetime
    properties: dict | None


@dataclass(frozen=True)
class PaidCheckoutRow:
    plan_code: str
    billing_cycle: str
    payment_method: str
    amount_cents: int
    paid_at: datetime


@dataclass(frozen=True)
class ActiveSubscriptionRow:
    billing_cycle: str
    amount_cents: int
    status: str
    access_until: datetime


def _local_day(value: datetime) -> date:
    return value.astimezone(ANALYTICS_TZ).date()


def _event_matches_landing_page(event: AnalyticsEventRow) -> bool:
    return event.event_name == "page_view" and (event.properties or {}).get("route_group") == "landing"


def _ratio(current: int | None, previous: int | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return round((current / previous) * 100, 1)


def _plan_label(value: str) -> str:
    return {"basic": "Basic", "plus": "Plus"}.get(value, value.title())


def _payment_label(value: str) -> str:
    return {
        "pix": "Pix",
        "pix_automatic": "Pix Automático",
        "credit_card": "Cartão",
    }.get(value, value.replace("_", " ").title())


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


def build_platform_analytics_summary(
    *,
    events: list[AnalyticsEventRow],
    paid_checkouts: list[PaidCheckoutRow],
    active_subscriptions: list[ActiveSubscriptionRow],
    now: datetime,
    days: int,
) -> PlatformAnalyticsSummaryResponse:
    period_start = now - timedelta(days=days)

    lpv = sum(1 for event in events if _event_matches_landing_page(event))
    vsl = sum(1 for event in events if event.event_name == "landing_vsl_started")
    cta_clicks = sum(1 for event in events if event.event_name == "landing_cta_click")
    accounts = sum(1 for event in events if event.event_name == "signup_completed")
    activations = sum(
        1 for event in events if event.event_name == "whatsapp_connection_completed"
    )
    subscriptions = len(paid_checkouts)
    revenue_cents = sum(checkout.amount_cents for checkout in paid_checkouts)
    mrr_cents = _mrr_cents(active_subscriptions, now)

    funnel = [
        PlatformAnalyticsFunnelStep(
            key="ad_clicks",
            label="Cliques",
            value=None,
            status="awaiting_integration",
            note="Aguardando integração de métricas do Meta Ads.",
        ),
        PlatformAnalyticsFunnelStep(
            key="lpv",
            label="LPV",
            value=lpv,
            status="ready",
            note="Landing Page View / visualização da página.",
        ),
        PlatformAnalyticsFunnelStep(
            key="vsl",
            label="VSL",
            value=vsl,
            conversion_rate=_ratio(vsl, lpv),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="accounts",
            label="Contas",
            value=accounts,
            conversion_rate=_ratio(accounts, vsl),
            status="ready",
        ),
        PlatformAnalyticsFunnelStep(
            key="activations",
            label="Ativações",
            value=activations,
            conversion_rate=_ratio(activations, accounts),
            status="ready",
            note="Ativação operacional = conexão do WhatsApp concluída.",
        ),
        PlatformAnalyticsFunnelStep(
            key="subscriptions",
            label="Assinaturas",
            value=subscriptions,
            conversion_rate=_ratio(subscriptions, activations),
            status="ready",
            note="Pagamentos confirmados no período.",
        ),
    ]

    kpis = [
        PlatformAnalyticsKpi(
            key="mrr",
            label="MRR",
            value=mrr_cents / 100,
            format="currency_brl",
            note="Receita recorrente mensal equivalente das assinaturas ativas.",
        ),
        PlatformAnalyticsKpi(
            key="revenue_received",
            label="Receita recebida",
            value=revenue_cents / 100,
            format="currency_brl",
            note="Pagamentos confirmados no período.",
        ),
        PlatformAnalyticsKpi(
            key="subscriptions",
            label="Assinaturas",
            value=subscriptions,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="activations",
            label="Ativações",
            value=activations,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="lpv",
            label="LPV",
            value=lpv,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="vsl_started",
            label="VSL iniciadas",
            value=vsl,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="cta_clicks",
            label="Cliques no CTA",
            value=cta_clicks,
            format="count",
        ),
        PlatformAnalyticsKpi(
            key="accounts",
            label="Contas criadas",
            value=accounts,
            format="count",
        ),
    ]

    plan_counts = Counter(checkout.plan_code for checkout in paid_checkouts)
    payment_counts = Counter(checkout.payment_method for checkout in paid_checkouts)

    placement_counts: Counter[str] = Counter()
    for event in events:
        if event.event_name != "payment_confirmed":
            continue
        placement = (event.properties or {}).get("placement")
        if isinstance(placement, str) and placement.strip():
            placement_counts[placement.strip()] += 1

    subscriptions_by_plan = PlatformAnalyticsBreakdown(
        key="subscriptions_by_plan",
        label="Assinaturas por plano",
        status="ready" if plan_counts else "insufficient_data",
        note=None if plan_counts else "Ainda não há assinaturas pagas no período selecionado.",
        items=[
            PlatformAnalyticsBreakdownItem(
                key=key,
                label=_plan_label(key),
                value=value,
            )
            for key, value in plan_counts.most_common()
        ],
    )
    subscriptions_by_payment_method = PlatformAnalyticsBreakdown(
        key="subscriptions_by_payment_method",
        label="Assinaturas por pagamento",
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
        label="Assinaturas por posicionamento",
        status="ready" if placement_counts else "awaiting_integration",
        note=(
            None
            if placement_counts
            else "Aguardando volume de conversões atribuídas com posicionamento do Meta Ads."
        ),
        items=[
            PlatformAnalyticsBreakdownItem(key=key, label=key, value=value)
            for key, value in placement_counts.most_common()
        ],
    )

    local_end = _local_day(now)
    local_start = _local_day(period_start)
    day_count = (local_end - local_start).days
    series_by_day: dict[date, dict[str, int]] = {
        local_start + timedelta(days=offset): {
            "lpv": 0,
            "vsl": 0,
            "accounts": 0,
            "activations": 0,
            "subscriptions": 0,
        }
        for offset in range(day_count + 1)
    }

    for event in events:
        event_day = _local_day(event.occurred_at)
        bucket = series_by_day.get(event_day)
        if bucket is None:
            continue
        if _event_matches_landing_page(event):
            bucket["lpv"] += 1
        elif event.event_name == "landing_vsl_started":
            bucket["vsl"] += 1
        elif event.event_name == "signup_completed":
            bucket["accounts"] += 1
        elif event.event_name == "whatsapp_connection_completed":
            bucket["activations"] += 1

    for checkout in paid_checkouts:
        checkout_day = _local_day(checkout.paid_at)
        bucket = series_by_day.get(checkout_day)
        if bucket is not None:
            bucket["subscriptions"] += 1

    series = [
        PlatformAnalyticsSeriesPoint(date=day, **values)
        for day, values in sorted(series_by_day.items())
    ]

    future_metrics = [
        PlatformAnalyticsKpi(
            key="ad_spend",
            label="Gasto com anúncios",
            value=None,
            format="currency_brl",
            status="awaiting_integration",
            note="Aguardando integração com Meta Ads.",
        ),
        PlatformAnalyticsKpi(
            key="ctr",
            label="CTR",
            value=None,
            format="percent",
            status="awaiting_integration",
            note="Click Through Rate / taxa de cliques.",
        ),
        PlatformAnalyticsKpi(
            key="cpc",
            label="CPC",
            value=None,
            format="currency_brl",
            status="awaiting_integration",
            note="Cost Per Click / custo por clique.",
        ),
        PlatformAnalyticsKpi(
            key="cpm",
            label="CPM",
            value=None,
            format="currency_brl",
            status="awaiting_integration",
            note="Cost Per Mille / custo por mil impressões.",
        ),
        PlatformAnalyticsKpi(
            key="cac",
            label="CAC",
            value=None,
            format="currency_brl",
            status="awaiting_integration",
            note="Customer Acquisition Cost / custo de aquisição de cliente.",
        ),
        PlatformAnalyticsKpi(
            key="roas",
            label="ROAS",
            value=None,
            format="ratio",
            status="awaiting_integration",
            note="Return on Ad Spend / retorno sobre investimento em anúncios.",
        ),
        PlatformAnalyticsKpi(
            key="roi",
            label="ROI",
            value=None,
            format="ratio",
            status="structuring",
            note="Return on Investment / retorno sobre investimento.",
        ),
        PlatformAnalyticsKpi(
            key="margin",
            label="Margem",
            value=None,
            format="percent",
            status="structuring",
            note="Aguardando definição consolidada de custos.",
        ),
        PlatformAnalyticsKpi(
            key="trial_to_paid",
            label="Teste → pago",
            value=None,
            format="percent",
            status="structuring",
            note="Será calculado por coorte para evitar uma taxa enganosa por volume do período.",
        ),
        PlatformAnalyticsKpi(
            key="churn",
            label="Churn",
            value=None,
            format="percent",
            status="awaiting_history",
            note="Cancelamento: aguardando base histórica suficiente.",
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
        series=series,
        future_metrics=future_metrics,
    )


class PlatformAnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def summary(self, days: int) -> PlatformAnalyticsSummaryResponse:
        now = datetime.now(UTC)
        period_start = now - timedelta(days=days)
        provider_environment = billing_provider_environment()

        event_rows = await self.db.execute(
            select(
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
                event_name=event_name,
                occurred_at=occurred_at,
                properties=properties,
            )
            for event_name, occurred_at, properties in event_rows
        ]

        paid_rows = await self.db.execute(
            select(
                BillingCheckout.plan_code,
                BillingCheckout.billing_cycle,
                BillingCheckout.payment_method,
                BillingCheckout.amount_cents,
                BillingCheckout.paid_at,
            ).where(
                BillingCheckout.provider_environment == provider_environment,
                BillingCheckout.status == "paid",
                BillingCheckout.paid_at.is_not(None),
                BillingCheckout.paid_at >= period_start,
            )
        )
        paid_checkouts = [
            PaidCheckoutRow(
                plan_code=plan_code,
                billing_cycle=billing_cycle,
                payment_method=payment_method,
                amount_cents=amount_cents,
                paid_at=paid_at,
            )
            for plan_code, billing_cycle, payment_method, amount_cents, paid_at in paid_rows
            if paid_at is not None
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

        return build_platform_analytics_summary(
            events=events,
            paid_checkouts=paid_checkouts,
            active_subscriptions=active_subscriptions,
            now=now,
            days=days,
        )

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetaAdInsight:
    campaign_id: str
    campaign_name: str
    adset_id: str
    adset_name: str
    ad_id: str
    ad_name: str
    impressions: int
    reach: int
    link_clicks: int
    spend_brl: float
    ctr: float | None
    cpc: float | None
    cpm: float | None


@dataclass(frozen=True, slots=True)
class MetaAdsReport:
    status: str
    note: str | None
    ads: list[MetaAdInsight]


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _integer(value: object) -> int:
    parsed = _decimal(value)
    if parsed is None:
        return 0
    try:
        return max(0, int(parsed))
    except (OverflowError, ValueError):
        return 0


def _float(value: object) -> float | None:
    parsed = _decimal(value)
    return float(parsed) if parsed is not None else None


async def fetch_meta_ads_report(
    settings: Settings,
    *,
    since: date,
    until: date,
) -> MetaAdsReport:
    configuration = settings.meta_ads_configuration()
    if configuration is None:
        return MetaAdsReport(
            status="awaiting_integration",
            note="Aguardando credencial de leitura da Meta Ads.",
            ads=[],
        )

    endpoint = (
        f"https://graph.facebook.com/{configuration.graph_version}/"
        f"act_{configuration.account_id}/insights"
    )
    params: dict[str, Any] = {
        "access_token": configuration.access_token.get_secret_value(),
        "level": "ad",
        "fields": ",".join(
            (
                "campaign_id",
                "campaign_name",
                "adset_id",
                "adset_name",
                "ad_id",
                "ad_name",
                "impressions",
                "reach",
                "spend",
                "inline_link_clicks",
                "inline_link_click_ctr",
                "cost_per_inline_link_click",
                "cpm",
            )
        ),
        "time_range": (
            '{"since":"%s","until":"%s"}'
            % (since.isoformat(), until.isoformat())
        ),
        "limit": "500",
    }

    rows: list[dict[str, Any]] = []
    after: str | None = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(10):
                request_params = dict(params)
                if after:
                    request_params["after"] = after
                response = await client.get(endpoint, params=request_params)
                if response.status_code >= 400:
                    logger.warning(
                        "meta_ads_insights_failed",
                        extra={"status_code": response.status_code},
                    )
                    return MetaAdsReport(
                        status="awaiting_integration",
                        note="A Meta Ads não liberou os insights agora.",
                        ads=[],
                    )
                payload = response.json()
                data = payload.get("data")
                if isinstance(data, list):
                    rows.extend(item for item in data if isinstance(item, dict))
                paging = payload.get("paging")
                cursors = paging.get("cursors") if isinstance(paging, dict) else None
                next_after = cursors.get("after") if isinstance(cursors, dict) else None
                if not isinstance(next_after, str) or not next_after or not data:
                    break
                after = next_after
    except (httpx.HTTPError, TimeoutError, ValueError):
        logger.warning("meta_ads_insights_failed")
        return MetaAdsReport(
            status="awaiting_integration",
            note="Não foi possível consultar os insights da Meta Ads.",
            ads=[],
        )

    prefix = configuration.campaign_name_prefix.casefold()
    ads: list[MetaAdInsight] = []
    for row in rows:
        campaign_name = str(row.get("campaign_name") or "").strip()
        if not campaign_name.casefold().startswith(prefix):
            continue
        ad_id = str(row.get("ad_id") or "").strip()
        if not ad_id:
            continue
        ads.append(
            MetaAdInsight(
                campaign_id=str(row.get("campaign_id") or "").strip(),
                campaign_name=campaign_name,
                adset_id=str(row.get("adset_id") or "").strip(),
                adset_name=str(row.get("adset_name") or "").strip(),
                ad_id=ad_id,
                ad_name=str(row.get("ad_name") or ad_id).strip(),
                impressions=_integer(row.get("impressions")),
                reach=_integer(row.get("reach")),
                link_clicks=_integer(row.get("inline_link_clicks")),
                spend_brl=_float(row.get("spend")) or 0.0,
                ctr=_float(row.get("inline_link_click_ctr")),
                cpc=_float(row.get("cost_per_inline_link_click")),
                cpm=_float(row.get("cpm")),
            )
        )

    return MetaAdsReport(
        status="ready",
        note=None,
        ads=ads,
    )

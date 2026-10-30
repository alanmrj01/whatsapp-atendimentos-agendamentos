from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import ipaddress
import logging
from typing import Any, Mapping
from uuid import UUID

import httpx

from app.core.config import MetaConversionsConfiguration, Settings
from app.models import BillingCheckout

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetaConversionEvent:
    event_name: str
    event_id: str
    event_time: int
    event_source_url: str
    user_data: dict[str, Any]
    custom_data: dict[str, Any] | None = None


def _sha256(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _email_hash(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    return _sha256(normalized) if normalized else None


def _external_id_hash(identifier: UUID | str | None) -> str | None:
    if identifier is None:
        return None
    normalized = str(identifier).strip().lower()
    return _sha256(normalized) if normalized else None


def _safe_meta_cookie(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 255 or not normalized.startswith("fb."):
        return None
    return normalized


def _derived_fbc(
    *,
    existing_fbc: str | None,
    fbclid: object,
    occurred_at: datetime,
) -> str | None:
    safe_existing = _safe_meta_cookie(existing_fbc)
    if safe_existing:
        return safe_existing

    if not isinstance(fbclid, str):
        return None
    normalized = fbclid.strip()
    if not normalized or len(normalized) > 200:
        return None

    return f"fb.1.{int(occurred_at.timestamp() * 1000)}.{normalized}"


def client_ip_address(
    forwarded_for: str | None,
    client_host: str | None,
) -> str | None:
    candidates: list[str] = []
    if forwarded_for:
        candidates.extend(part.strip() for part in forwarded_for.split(","))
    if client_host:
        candidates.append(client_host.strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def _base_user_data(
    *,
    visitor_id: UUID | None,
    user_id: UUID | None,
    user_email: str | None,
    fbp: str | None,
    fbc: str | None,
    client_ip: str | None,
    client_user_agent: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}

    external_id = _external_id_hash(user_id or visitor_id)
    if external_id:
        data["external_id"] = [external_id]

    email = _email_hash(user_email)
    if email:
        data["em"] = [email]

    safe_fbp = _safe_meta_cookie(fbp)
    safe_fbc = _safe_meta_cookie(fbc)
    if safe_fbp:
        data["fbp"] = safe_fbp
    if safe_fbc:
        data["fbc"] = safe_fbc

    if client_ip:
        data["client_ip_address"] = client_ip

    user_agent = (client_user_agent or "").strip()
    if user_agent:
        data["client_user_agent"] = user_agent[:512]

    return data


def _marketing_meta_name(
    event_name: str,
    properties: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None] | None:
    if event_name == "page_view":
        if properties.get("route_group") not in {"landing", "signup", "checkout", "plan"}:
            return None
        return "PageView", None

    if event_name == "landing_vsl_started":
        return "VSLStarted", {"video_id": str(properties.get("video_id") or "")[:64]}

    if event_name == "landing_vsl_progress":
        percent = properties.get("percent")
        if percent not in {25, 50, 75}:
            return None
        return "VSLProgress", {"progress_percent": int(percent)}

    if event_name == "landing_vsl_completed":
        return "VSLCompleted", {"progress_percent": 100}

    if event_name == "signup_completed":
        return "CompleteRegistration", {"content_name": "alovia_account"}

    if event_name == "whatsapp_connection_completed":
        custom_data: dict[str, Any] = {}
        mode = properties.get("mode")
        if isinstance(mode, str) and mode.strip():
            custom_data["connection_mode"] = mode.strip()[:64]
        return "WhatsAppConnected", custom_data or None

    if event_name == "checkout_started":
        custom_data = {"content_category": "saas_subscription"}
        plan = properties.get("plan")
        cycle = properties.get("billing_cycle")
        if isinstance(plan, str) and plan.strip():
            custom_data["content_name"] = plan.strip()[:64]
        if isinstance(cycle, str) and cycle.strip():
            custom_data["billing_cycle"] = cycle.strip()[:32]
        return "InitiateCheckout", custom_data

    # payment_confirmed is intentionally excluded here. Purchase is emitted only
    # from the authoritative Asaas webhook path.
    return None


def marketing_conversion_event(
    settings: Settings,
    *,
    event_name: str,
    event_id: UUID,
    occurred_at: datetime,
    path: str,
    properties: Mapping[str, Any] | None,
    visitor_id: UUID,
    user_id: UUID | None,
    user_email: str | None,
    fbp: str | None,
    fbc: str | None,
    client_ip: str | None,
    client_user_agent: str | None,
) -> MetaConversionEvent | None:
    configuration = settings.meta_conversions_configuration()
    if configuration is None:
        return None

    safe_properties = properties or {}
    mapped = _marketing_meta_name(event_name, safe_properties)
    if mapped is None:
        return None
    meta_name, custom_data = mapped

    safe_path = path if path.startswith("/") else "/"
    return MetaConversionEvent(
        event_name=meta_name,
        event_id=str(event_id),
        event_time=int(occurred_at.timestamp()),
        event_source_url=f"{configuration.event_source_base_url}{safe_path}",
        user_data=_base_user_data(
            visitor_id=visitor_id,
            user_id=user_id,
            user_email=user_email,
            fbp=fbp,
            fbc=_derived_fbc(
                existing_fbc=fbc,
                fbclid=safe_properties.get("fbclid"),
                occurred_at=occurred_at,
            ),
            client_ip=client_ip,
            client_user_agent=client_user_agent,
        ),
        custom_data=custom_data,
    )


def purchase_conversion_event(
    settings: Settings,
    *,
    checkout: BillingCheckout,
    occurred_at: datetime,
    owner_user_id: UUID | None,
    owner_email: str | None,
) -> MetaConversionEvent | None:
    configuration = settings.meta_conversions_configuration()
    if (
        configuration is None
        or checkout.provider_environment != "production"
        or checkout.status != "paid"
    ):
        return None

    user_data: dict[str, Any] = {}
    external_id = _external_id_hash(owner_user_id)
    if external_id:
        user_data["external_id"] = [external_id]
    email = _email_hash(owner_email)
    if email:
        user_data["em"] = [email]

    return MetaConversionEvent(
        event_name="Purchase",
        event_id=str(checkout.id),
        event_time=int(occurred_at.timestamp()),
        event_source_url=f"{configuration.event_source_base_url}/app/checkout/retorno",
        user_data=user_data,
        custom_data={
            "currency": "BRL",
            "value": round(checkout.amount_cents / 100, 2),
            "order_id": str(checkout.id),
            "content_name": checkout.plan_code,
            "content_type": "product",
            "content_ids": [checkout.plan_code],
        },
    )


def build_meta_payload(
    configuration: MetaConversionsConfiguration,
    event: MetaConversionEvent,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "event_name": event.event_name,
        "event_time": event.event_time,
        "event_id": event.event_id,
        "action_source": "website",
        "event_source_url": event.event_source_url,
        "user_data": event.user_data,
    }
    if event.custom_data:
        item["custom_data"] = event.custom_data

    payload: dict[str, Any] = {
        "data": [item],
        "access_token": configuration.access_token.get_secret_value(),
    }
    if configuration.test_event_code:
        payload["test_event_code"] = configuration.test_event_code
    return payload


async def capture_meta_conversion(
    settings: Settings,
    event: MetaConversionEvent,
) -> None:
    configuration = settings.meta_conversions_configuration()
    if configuration is None:
        return

    payload = build_meta_payload(configuration, event)
    endpoint = (
        f"https://graph.facebook.com/{configuration.graph_version}/"
        f"{configuration.dataset_id}/events"
    )

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.post(endpoint, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "meta_capi_capture_failed",
                extra={
                    "status_code": response.status_code,
                    "event_name": event.event_name,
                },
            )
    except (httpx.HTTPError, TimeoutError):
        logger.warning(
            "meta_capi_capture_failed",
            extra={"event_name": event.event_name},
        )

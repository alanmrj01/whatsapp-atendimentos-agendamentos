from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.models import Service
from app.conversations.service_semantics import generate_service_intent_examples


@dataclass(frozen=True, slots=True)
class DefaultOperationalService:
    key: str
    name: str
    duration_minutes: int


DEFAULT_OPERATIONAL_SERVICES = (
    DefaultOperationalService(
        "split-installation",
        "Instalação de ar-condicionado split",
        180,
    ),
    DefaultOperationalService(
        "cleaning",
        "Limpeza e higienização",
        90,
    ),
    DefaultOperationalService(
        "preventive-maintenance",
        "Manutenção preventiva",
        90,
    ),
    DefaultOperationalService(
        "diagnostics",
        "Diagnóstico / manutenção corretiva",
        120,
    ),
    DefaultOperationalService(
        "gas-recharge",
        "Recarga de gás e teste de vazamento",
        120,
    ),
)


def default_services_for_business(business_id: UUID) -> list[Service]:
    """Build editable, price-free defaults for one newly-created tenant."""

    return [
        Service(
            business_id=business_id,
            name=item.name,
            duration_minutes=item.duration_minutes,
            base_price=None,
            pricing_type="estimated",
            automatic_booking=True,
            requires_address=True,
            asks_tubing_length=item.key == "split-installation",
            included_tubing_meters=3 if item.key == "split-installation" else None,
            intent_examples=list(generate_service_intent_examples(item.name)),
            active=True,
        )
        for item in DEFAULT_OPERATIONAL_SERVICES
    ]

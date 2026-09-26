from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.equipment_recommender import equipment_catalog_presets
from app.models import BusinessCatalogItem

MATERIAL_PRESETS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "extra-tubing-meter",
        "material",
        "Metro adicional de tubulação",
        "Cobrança por metro acima da metragem incluída no serviço.",
        "metro",
    ),
    (
        "extra-drain-meter",
        "material",
        "Metro adicional de dreno",
        "Material adicional de drenagem quando necessário.",
        "metro",
    ),
    (
        "extra-electrical-cable-meter",
        "material",
        "Metro adicional de cabo elétrico",
        "Cabo elétrico adicional utilizado na instalação.",
        "metro",
    ),
    (
        "condenser-bracket",
        "equipment",
        "Suporte para condensadora",
        "Suporte utilizado na instalação da unidade externa.",
        "unidade",
    ),
    (
        "wall-bracket-fixings",
        "material",
        "Kit de fixação",
        "Parafusos, buchas e itens de fixação adicionais.",
        "kit",
    ),
)


def catalog_preset_definitions() -> tuple[tuple[str, str, str, str, str], ...]:
    equipment = tuple(
        (
            f"equipment:{item['id']}",
            "equipment",
            (
                f"{item['brand']} {item['line']} "
                f"{int(item['capacity_btu']):,} BTU"
            ).replace(",", "."),
            (
                "Referência comercial inicial do catálogo Alovia; preço e "
                "estoque devem ser confirmados pela empresa."
            ),
            "unidade",
        )
        for item in equipment_catalog_presets()
    )
    return (*MATERIAL_PRESETS, *equipment)


def catalog_preset_items(business_id: UUID) -> list[BusinessCatalogItem]:
    return [
        BusinessCatalogItem(
            business_id=business_id,
            preset_key=key,
            kind=kind,
            name=name,
            description=description,
            unit_label=unit_label,
            active=True,
        )
        for key, kind, name, description, unit_label in catalog_preset_definitions()
    ]


async def ensure_business_catalog_presets(
    session: AsyncSession,
    business_id: UUID,
) -> bool:
    existing = set(
        (
            await session.scalars(
                select(BusinessCatalogItem.preset_key).where(
                    BusinessCatalogItem.business_id == business_id,
                    BusinessCatalogItem.preset_key.is_not(None),
                )
            )
        ).all()
    )
    missing = [
        item
        for item in catalog_preset_items(business_id)
        if item.preset_key not in existing
    ]
    if not missing:
        return False
    session.add_all(missing)
    return True

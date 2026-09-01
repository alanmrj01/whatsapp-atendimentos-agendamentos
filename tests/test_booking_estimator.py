from dataclasses import replace
from decimal import Decimal

from app.booking.domain import (
    AccessCondition,
    BookingRequirements,
    PricingType,
    ServiceAddress,
    ServiceConfiguration,
    UnknownAccessPolicy,
)
from app.booking.estimator import ServiceEstimator


def configuration(**changes: object) -> ServiceConfiguration:
    values = ServiceConfiguration(
        duration_minutes=120,
        base_price=Decimal("300.00"),
        pricing_type=PricingType.ESTIMATED,
        automatic_booking=True,
        included_quantity=1,
        additional_unit_duration_minutes=45,
        additional_unit_price=Decimal("80.00"),
        requires_address=True,
        requires_quantity=True,
        considers_difficult_access=True,
        difficult_access_duration_minutes=30,
        difficult_access_price=Decimal("50.00"),
        unknown_access_policy=UnknownAccessPolicy.CONSERVATIVE,
        duration_margin_minutes=15,
    )
    return replace(values, **changes)


def requirements(**changes: object) -> BookingRequirements:
    values = BookingRequirements(
        quantity=1,
        access_condition=AccessCondition.NORMAL,
        address=ServiceAddress("Rua Exemplo, 10, São José dos Campos - SP"),
    )
    return replace(values, **changes)


def test_quantity_changes_price_and_duration_generically() -> None:
    estimate = ServiceEstimator().estimate(
        configuration(),
        requirements(quantity=3),
    )

    assert estimate.estimated_duration_minutes == 225
    assert estimate.estimated_price == Decimal("460.00")
    assert "additional_units" in estimate.applied_rules


def test_difficult_access_changes_price_and_duration() -> None:
    estimate = ServiceEstimator().estimate(
        configuration(),
        requirements(access_condition=AccessCondition.DIFFICULT),
    )

    assert estimate.estimated_duration_minutes == 165
    assert estimate.estimated_price == Decimal("350.00")
    assert "difficult_access" in estimate.applied_rules


def test_unknown_access_uses_configured_conservative_rule() -> None:
    estimate = ServiceEstimator().estimate(
        configuration(),
        requirements(access_condition=AccessCondition.UNKNOWN),
    )

    assert estimate.requires_human_quote is False
    assert estimate.estimated_duration_minutes == 165
    assert "unknown_access_conservative" in estimate.applied_rules


def test_unknown_access_can_require_human_quote() -> None:
    estimate = ServiceEstimator().estimate(
        configuration(unknown_access_policy=UnknownAccessPolicy.HUMAN_QUOTE),
        requirements(access_condition=AccessCondition.UNKNOWN),
    )

    assert estimate.requires_human_quote is True
    assert estimate.estimated_price is None
    assert estimate.applied_rules == ("unknown_access",)


def test_fixed_and_estimated_price_types_are_preserved() -> None:
    estimator = ServiceEstimator()

    fixed = estimator.estimate(
        configuration(pricing_type=PricingType.FIXED), requirements()
    )
    estimated = estimator.estimate(configuration(), requirements())

    assert fixed.pricing_type is PricingType.FIXED
    assert fixed.qualifier == "fixed"
    assert estimated.pricing_type is PricingType.ESTIMATED
    assert estimated.qualifier == "estimated"


def test_missing_indispensable_address_requires_handoff() -> None:
    estimate = ServiceEstimator().estimate(
        configuration(),
        requirements(address=None),
    )

    assert estimate.requires_human_quote is True
    assert estimate.applied_rules == ("address_required",)


def test_missing_quantity_or_unsafe_price_requires_handoff() -> None:
    estimator = ServiceEstimator()

    missing_quantity = estimator.estimate(
        configuration(), requirements(quantity=None)
    )
    missing_price = estimator.estimate(
        configuration(base_price=None), requirements()
    )

    assert missing_quantity.requires_human_quote is True
    assert missing_price.requires_human_quote is True
    assert missing_price.estimated_price is None


def test_human_quote_and_nonautomatic_services_never_auto_book() -> None:
    estimator = ServiceEstimator()

    human_quote = estimator.estimate(
        configuration(pricing_type=PricingType.HUMAN_QUOTE), requirements()
    )
    nonautomatic = estimator.estimate(
        configuration(automatic_booking=False), requirements()
    )

    assert human_quote.requires_human_quote is True
    assert nonautomatic.requires_human_quote is True

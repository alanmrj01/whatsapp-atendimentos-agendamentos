from __future__ import annotations

from decimal import Decimal

from app.booking.domain import (
    AccessCondition,
    BookingRequirements,
    PricingType,
    ServiceConfiguration,
    ServiceEstimate,
    UnknownAccessPolicy,
)


class ServiceEstimator:
    """Calcula preço e duração somente a partir de regras persistidas."""

    def estimate(
        self,
        configuration: ServiceConfiguration,
        requirements: BookingRequirements,
    ) -> ServiceEstimate:
        quantity = requirements.quantity
        rules: list[str] = ["base_duration"]

        if configuration.requires_quantity and quantity is None:
            return self._human_quote(configuration, "quantity_required")
        quantity = quantity or configuration.included_quantity
        additional_units = max(0, quantity - configuration.included_quantity)

        duration = configuration.duration_minutes
        duration += additional_units * configuration.additional_unit_duration_minutes
        if additional_units:
            rules.append("additional_units")

        price = configuration.base_price
        if price is not None and additional_units:
            additional_price = configuration.additional_unit_price or Decimal("0")
            price += additional_price * additional_units

        if configuration.considers_difficult_access:
            access = requirements.access_condition
            if access is AccessCondition.UNKNOWN:
                if configuration.unknown_access_policy is UnknownAccessPolicy.HUMAN_QUOTE:
                    return self._human_quote(configuration, "unknown_access")
                if configuration.unknown_access_policy is UnknownAccessPolicy.CONSERVATIVE:
                    access = AccessCondition.DIFFICULT
                    rules.append("unknown_access_conservative")
                else:
                    rules.append("unknown_access_standard")
            if access is AccessCondition.DIFFICULT:
                duration += configuration.difficult_access_duration_minutes
                if price is not None:
                    price += configuration.difficult_access_price or Decimal("0")
                rules.append("difficult_access")

        duration += configuration.duration_margin_minutes
        if configuration.duration_margin_minutes:
            rules.append("duration_margin")

        if configuration.requires_address and requirements.address is None:
            return self._human_quote(configuration, "address_required")
        if (
            not configuration.automatic_booking
            or configuration.pricing_type is PricingType.HUMAN_QUOTE
            or price is None
        ):
            return self._human_quote(configuration, "human_quote_configuration")

        qualifier = (
            "fixed"
            if configuration.pricing_type is PricingType.FIXED
            else "estimated"
        )
        return ServiceEstimate(
            estimated_duration_minutes=duration,
            estimated_price=price,
            pricing_type=configuration.pricing_type,
            requires_human_quote=False,
            applied_rules=tuple(rules),
            qualifier=qualifier,
        )

    @staticmethod
    def _human_quote(
        configuration: ServiceConfiguration,
        reason: str,
    ) -> ServiceEstimate:
        return ServiceEstimate(
            estimated_duration_minutes=(
                configuration.duration_minutes
                + configuration.duration_margin_minutes
            ),
            estimated_price=None,
            pricing_type=configuration.pricing_type,
            requires_human_quote=True,
            applied_rules=(reason,),
            qualifier="human_quote",
        )

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.availability import PostgresBookingAvailabilityPort
from app.booking.google_maps import (
    GoogleAddressValidationPort,
    GoogleMapsAccessTokenProvider,
    GoogleRoutesTravelTimePort,
)
from app.conversations.ports import BookingAvailabilityPort
from app.core.config import Settings, get_settings
from app.core.database import get_db


def get_booking_availability_port(
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BookingAvailabilityPort:
    """Usa a mesma sessão/transação resolvida para o webhook atual."""
    if (
        settings.google_maps_routing_enabled
        and settings.gcp_project_id
        and settings.gcp_project_id.strip()
    ):
        token_provider = GoogleMapsAccessTokenProvider()
        address_validation_port = GoogleAddressValidationPort(
            project_id=settings.gcp_project_id.strip(),
            token_provider=token_provider,
            timeout_seconds=settings.google_maps_request_timeout_seconds,
        )
        travel_time_port = GoogleRoutesTravelTimePort(
            project_id=settings.gcp_project_id.strip(),
            token_provider=token_provider,
            address_validation_port=address_validation_port,
            timeout_seconds=settings.google_maps_request_timeout_seconds,
        )
        return PostgresBookingAvailabilityPort(
            session,
            address_validation_port=address_validation_port,
            travel_time_port=travel_time_port,
        )
    return PostgresBookingAvailabilityPort(session)

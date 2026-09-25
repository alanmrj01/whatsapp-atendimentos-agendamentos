from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.availability import PostgresBookingAvailabilityPort
from app.booking.travel import GoogleRoutesTravelTimePort, TravelTimePort
from app.conversations.ports import BookingAvailabilityPort
from app.core.config import get_settings
from app.core.database import get_db


@lru_cache(maxsize=1)
def _google_routes_port() -> TravelTimePort | None:
    settings = get_settings()
    project_id = (settings.gcp_project_id or "").strip()
    if not project_id:
        return None
    return GoogleRoutesTravelTimePort(project_id)


def get_booking_availability_port(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BookingAvailabilityPort:
    """Usa a mesma sessão/transação resolvida para o webhook atual."""
    return PostgresBookingAvailabilityPort(
        session,
        travel_time_port=_google_routes_port(),
    )

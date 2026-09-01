from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking.availability import PostgresBookingAvailabilityPort
from app.conversations.ports import BookingAvailabilityPort
from app.core.database import get_db


def get_booking_availability_port(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> BookingAvailabilityPort:
    """Usa a mesma sessão/transação resolvida para o webhook atual."""
    return PostgresBookingAvailabilityPort(session)

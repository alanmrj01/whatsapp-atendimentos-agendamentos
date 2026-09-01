from app.conversations.ports import BookingAvailabilityPort


def get_booking_availability_port() -> BookingAvailabilityPort | None:
    """Ponto de injeção do adapter PostgreSQL previsto para a Etapa 10."""
    return None

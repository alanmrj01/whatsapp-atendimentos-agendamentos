from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BookingOption:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class BookingConfirmation:
    appointment_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    employee_id: uuid.UUID

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("Booking confirmation end must be after start")


class BookingPortUnavailable(RuntimeError):
    """O adapter de disponibilidade não foi configurado."""


class SlotUnavailable(RuntimeError):
    """O horário selecionado não pode mais ser confirmado."""


class BookingAvailabilityPort(Protocol):
    async def list_services(
        self,
        business_id: uuid.UUID,
    ) -> Sequence[BookingOption]: ...

    async def list_dates(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
    ) -> Sequence[BookingOption]: ...

    async def list_times(
        self,
        business_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
    ) -> Sequence[BookingOption]: ...

    async def confirm(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        selected_time: str,
    ) -> BookingConfirmation: ...

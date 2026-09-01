from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BookingOption:
    id: str
    label: str


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
    ) -> None: ...

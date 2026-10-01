from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models import Appointment, Conversation
from app.operations.rescheduling import (
    _customer_service_window_is_open,
    _prepare_customer_reschedule,
    _require_open_customer_service_window,
    initiate_admin_reschedule,
)
from app.operations.schemas import AppointmentRescheduleRequest


@pytest.mark.asyncio
async def test_admin_reschedule_requires_open_customer_service_window() -> None:
    business_id = uuid4()
    customer_id = uuid4()
    conversation_id = uuid4()
    open_session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                conversation_id,
                datetime.now(UTC) - timedelta(hours=23),
            ]
        )
    )
    closed_session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                conversation_id,
                datetime.now(UTC) - timedelta(hours=24, seconds=1),
            ]
        )
    )

    assert await _customer_service_window_is_open(
        open_session,
        business_id=business_id,
        customer_id=customer_id,
    )
    with pytest.raises(HTTPException) as caught:
        await _require_open_customer_service_window(
            closed_session,
            business_id=business_id,
            customer_id=customer_id,
        )

    assert caught.value.status_code == 409
    assert "approved template" in caught.value.detail


@pytest.mark.asyncio
async def test_closed_window_leaves_confirmed_appointment_unchanged() -> None:
    business_id = uuid4()
    customer_id = uuid4()
    appointment = Appointment(
        id=uuid4(),
        business_id=business_id,
        customer_id=customer_id,
        status="confirmed",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                appointment,
                uuid4(),
                datetime.now(UTC) - timedelta(hours=25),
            ]
        ),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    booking_port = SimpleNamespace(list_customer_bookings=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await initiate_admin_reschedule(
            session,
            business_id=business_id,
            appointment_id=appointment.id,
            payload=AppointmentRescheduleRequest(),
            booking_port=booking_port,
        )

    assert caught.value.status_code == 409
    assert appointment.status == "confirmed"
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    booking_port.list_customer_bookings.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_reschedule_message_uses_stable_request_marker() -> None:
    business_id = uuid4()
    customer_id = uuid4()
    appointment_id = uuid4()
    conversation = Conversation(
        id=uuid4(),
        business_id=business_id,
        customer_id=customer_id,
        state="MENU",
        context={},
        handoff_status="none",
    )
    request_marker = "2026-10-01T12:00:00+00:00"
    appointment = Appointment(
        id=appointment_id,
        business_id=business_id,
        customer_id=customer_id,
        estimate_details={"reschedule_requested_at": request_marker},
        updated_at=datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[conversation, None]),
        add=Mock(),
        flush=AsyncMock(),
    )

    message = await _prepare_customer_reschedule(
        session,
        appointment=appointment,
        context={"appointment_id": str(appointment_id)},
        timezone_name="America/Sao_Paulo",
        preferred_starts_at=None,
        displaced=False,
    )

    assert message is not None
    assert message.idempotency_key == (
        f"admin-reschedule:{business_id}:{appointment_id}:{request_marker}"
    )
    assert appointment.updated_at.isoformat() not in message.idempotency_key

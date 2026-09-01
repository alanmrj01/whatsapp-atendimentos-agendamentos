from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from pytest import MonkeyPatch, mark, raises

from app.conversations.constants import ConversationState
from app.conversations.engine import (
    ConversationEngine,
    build_outbound_idempotency_key,
)
from app.conversations.ports import (
    BookingConfirmation,
    BookingOption,
    SlotUnavailable,
)
from app.conversations.types import (
    ConversationInput,
    ConversationSnapshot,
    ConversationTransition,
)
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.processor import process_webhook_events
from app.whatsapp.webhook import InboundMessageEvent, build_event_key

BUSINESS_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
CUSTOMER_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
CONVERSATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")
SERVICE_ID = uuid.UUID("40000000-0000-0000-0000-000000000004")
APPOINTMENT_ID = uuid.UUID("50000000-0000-0000-0000-000000000005")
EMPLOYEE_ID = uuid.UUID("60000000-0000-0000-0000-000000000006")


@dataclass(frozen=True, slots=True)
class StoredOutbound:
    transition: ConversationTransition
    idempotency_key: str
    status: str = "pending"
    provider_message_id: str | None = None


class FakeConversationRepository:
    def __init__(
        self,
        *,
        state: str = ConversationState.START,
        context: dict[str, Any] | None = None,
        automation_enabled: bool = True,
        handoff_status: str = "none",
    ) -> None:
        self.state = state
        self.context = context or {}
        self.automation_enabled = automation_enabled
        self.handoff_status = handoff_status
        self.outbounds: list[StoredOutbound] = []
        self.idempotency_keys: set[str] = set()
        self.lock = asyncio.Lock()
        self.fail_after_outbound = False

    @asynccontextmanager
    async def lock_conversation(
        self,
        business_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> AsyncIterator[ConversationSnapshot]:
        assert business_id == BUSINESS_ID
        assert conversation_id == CONVERSATION_ID
        async with self.lock:
            yield self.snapshot()

    async def outbound_exists(self, idempotency_key: str) -> bool:
        return idempotency_key in self.idempotency_keys

    async def persist_transition(
        self,
        _: ConversationSnapshot,
        transition: ConversationTransition,
        idempotency_key: str,
    ) -> bool:
        if idempotency_key in self.idempotency_keys:
            return False
        self.idempotency_keys.add(idempotency_key)
        self.outbounds.append(StoredOutbound(transition, idempotency_key))
        if self.fail_after_outbound:
            raise RuntimeError("simulated persistence failure")
        self.state = transition.state.value
        self.context = copy.deepcopy(transition.context)
        self.automation_enabled = transition.automation_enabled
        self.handoff_status = transition.handoff_status
        return True

    def snapshot(self) -> ConversationSnapshot:
        return ConversationSnapshot(
            business_id=BUSINESS_ID,
            customer_id=CUSTOMER_ID,
            conversation_id=CONVERSATION_ID,
            state=self.state,
            context=copy.deepcopy(self.context),
            automation_enabled=self.automation_enabled,
            handoff_status=self.handoff_status,
        )

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "state": self.state,
                "context": self.context,
                "automation_enabled": self.automation_enabled,
                "handoff_status": self.handoff_status,
                "outbounds": self.outbounds,
                "idempotency_keys": self.idempotency_keys,
            }
        )

    def restore_state(self, state: dict[str, Any]) -> None:
        self.state = state["state"]
        self.context = state["context"]
        self.automation_enabled = state["automation_enabled"]
        self.handoff_status = state["handoff_status"]
        self.outbounds = state["outbounds"]
        self.idempotency_keys = state["idempotency_keys"]


class FakeBookingPort:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.confirmations: list[tuple[Any, ...]] = []
        self.services = [BookingOption(str(SERVICE_ID), "Service")]
        self.dates = [BookingOption("2026-09-02", "02/09/2026")]
        self.times = [BookingOption("09:00", "09:00")]
        starts_at = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        self.confirmation: BookingConfirmation | None = BookingConfirmation(
            appointment_id=APPOINTMENT_ID,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            employee_id=EMPLOYEE_ID,
        )
        self.slot_unavailable = False

    async def list_services(self, _: uuid.UUID) -> tuple[BookingOption, ...]:
        self.calls.append("services")
        return tuple(self.services)

    async def list_dates(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
    ) -> tuple[BookingOption, ...]:
        assert service_id == SERVICE_ID
        self.calls.append("dates")
        return tuple(self.dates)

    async def list_times(
        self,
        _: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
    ) -> tuple[BookingOption, ...]:
        assert service_id == SERVICE_ID
        assert selected_date == "2026-09-02"
        self.calls.append("times")
        return tuple(self.times)

    async def confirm(
        self,
        business_id: uuid.UUID,
        customer_id: uuid.UUID,
        service_id: uuid.UUID,
        selected_date: str,
        selected_time: str,
    ) -> BookingConfirmation | None:
        self.calls.append("confirm")
        self.confirmations.append(
            (
                business_id,
                customer_id,
                service_id,
                selected_date,
                selected_time,
            )
        )
        if self.slot_unavailable:
            raise SlotUnavailable("slot is no longer available")
        return self.confirmation


def inbound(
    sequence: int,
    *,
    action: str | None = None,
    body: str | None = None,
) -> ConversationInput:
    return ConversationInput(
        business_id=BUSINESS_ID,
        customer_id=CUSTOMER_ID,
        conversation_id=CONVERSATION_ID,
        provider_message_id=f"provider-{sequence}",
        message_type="interactive" if action else "text",
        body=body,
        interactive_id=action,
    )


@mark.asyncio
async def test_full_booking_flow_persists_canonical_states_and_context() -> None:
    repository = FakeConversationRepository()
    booking_port = FakeBookingPort()
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, body="olá")) is True
    assert repository.state == ConversationState.MENU

    assert await engine.process(inbound(2, action="menu.book")) is True
    assert repository.state == ConversationState.BOOKING_SERVICE

    assert await engine.process(
        inbound(3, action=f"service:{SERVICE_ID}")
    ) is True
    assert repository.state == ConversationState.BOOKING_DATE
    assert repository.context == {"service_id": str(SERVICE_ID)}

    assert await engine.process(
        inbound(4, action="date:2026-09-02")
    ) is True
    assert repository.state == ConversationState.BOOKING_TIME
    assert repository.context == {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
    }

    assert await engine.process(inbound(5, action="time:09:00")) is True
    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context == {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
        "candidate_booking": {
            "service_id": str(SERVICE_ID),
            "selected_date": "2026-09-02",
            "selected_time": "09:00",
        },
    }

    assert await engine.process(inbound(6, action="booking.confirm")) is True
    assert repository.state == ConversationState.COMPLETED
    assert repository.context == {}
    assert booking_port.confirmations == [
        (
            BUSINESS_ID,
            CUSTOMER_ID,
            SERVICE_ID,
            "2026-09-02",
            "09:00",
        )
    ]
    message_types = [
        outbound.transition.outbound.message_type
        for outbound in repository.outbounds
    ]
    assert message_types == [
        "interactive_list",
        "interactive_list",
        "interactive_list",
        "interactive_list",
        "interactive_button",
        "text",
    ]
    payloads = [
        outbound.transition.outbound.outbound_payload
        for outbound in repository.outbounds
    ]
    assert [
        row["id"] for row in payloads[0]["sections"][0]["rows"]
    ] == [
        "menu.book",
        "menu.reschedule",
        "menu.cancel",
        "menu.human",
    ]
    assert payloads[1]["sections"][0]["rows"] == [
        {"id": f"service:{SERVICE_ID}", "title": "Service"}
    ]
    assert payloads[2]["sections"][0]["rows"] == [
        {"id": "date:2026-09-02", "title": "02/09/2026"}
    ]
    assert payloads[3]["sections"][0]["rows"] == [
        {"id": "time:09:00", "title": "09:00"}
    ]
    assert [button["id"] for button in payloads[4]["buttons"]] == [
        "booking.confirm",
        "booking.back",
        "booking.cancel",
    ]
    assert payloads[5] is None


@mark.asyncio
async def test_outbound_payload_is_snapshot_of_booking_options() -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    booking_port = FakeBookingPort()
    booking_port.services = [BookingOption(str(SERVICE_ID), "Original")]
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="menu.book")) is True
    stored_payload = copy.deepcopy(
        repository.outbounds[0].transition.outbound.outbound_payload
    )

    booking_port.services[0] = BookingOption(str(SERVICE_ID), "Changed")

    stored_outbound = repository.outbounds[0].transition.outbound
    assert stored_outbound.outbound_payload == stored_payload
    assert stored_payload["sections"][0]["rows"][0]["title"] == "Original"


@mark.parametrize(
    ("state", "context", "empty_collection", "action", "expected_state"),
    [
        (
            ConversationState.MENU,
            {},
            "services",
            "menu.book",
            ConversationState.MENU,
        ),
        (
            ConversationState.BOOKING_SERVICE,
            {},
            "dates",
            f"service:{SERVICE_ID}",
            ConversationState.BOOKING_SERVICE,
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "times",
            "date:2026-09-02",
            ConversationState.BOOKING_DATE,
        ),
    ],
)
@mark.asyncio
async def test_empty_availability_does_not_advance_to_impossible_choice(
    state: ConversationState,
    context: dict[str, Any],
    empty_collection: str,
    action: str,
    expected_state: ConversationState,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    booking_port = FakeBookingPort()
    setattr(booking_port, empty_collection, [])
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert len(repository.outbounds) == 1


@mark.parametrize(
    ("state", "context", "action"),
    [
        (ConversationState.MENU, {}, "menu.book"),
        (ConversationState.BOOKING_SERVICE, {}, f"service:{SERVICE_ID}"),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "date:2026-09-02",
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
            "time:09:00",
        ),
        (
            ConversationState.BOOKING_CONFIRM,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
                "selected_time": "09:00",
            },
            "booking.confirm",
        ),
    ],
)
@mark.asyncio
async def test_missing_booking_port_is_fail_closed(
    state: ConversationState,
    context: dict[str, Any],
    action: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    engine = ConversationEngine(repository, booking_port=None)

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == state
    assert repository.state != ConversationState.COMPLETED
    assert repository.context == context
    assert repository.outbounds[0].transition.outbound.message_type == "text"
    assert repository.outbounds[0].transition.outbound.outbound_payload is None


def confirmation_context() -> dict[str, Any]:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    return {**candidate, "candidate_booking": candidate}


@mark.asyncio
async def test_invalid_confirmation_result_does_not_complete_booking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
    )
    booking_port = FakeBookingPort()
    booking_port.confirmation = None
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="booking.confirm")) is True

    assert repository.state == ConversationState.BOOKING_CONFIRM
    assert repository.context == confirmation_context()


@mark.asyncio
async def test_slot_unavailable_does_not_complete_booking() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context=confirmation_context(),
    )
    booking_port = FakeBookingPort()
    booking_port.slot_unavailable = True
    engine = ConversationEngine(repository, booking_port)

    assert await engine.process(inbound(1, action="booking.confirm")) is True

    assert repository.state == ConversationState.BOOKING_CONFIRM
    payload = repository.outbounds[0].transition.outbound.outbound_payload
    assert [button["id"] for button in payload["buttons"]] == [
        "booking.back",
        "booking.cancel",
    ]


@mark.parametrize(
    ("action", "expected_state"),
    [
        ("menu.reschedule", ConversationState.RESCHEDULE),
        ("menu.cancel", ConversationState.CANCEL),
    ],
)
@mark.asyncio
async def test_menu_secondary_routes(
    action: str,
    expected_state: ConversationState,
) -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert repository.context == {}


@mark.asyncio
async def test_handoff_creates_last_outbound_then_disables_automation() -> None:
    repository = FakeConversationRepository(state=ConversationState.MENU)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action="menu.human")) is True
    assert repository.state == ConversationState.HUMAN_HANDOFF
    assert repository.automation_enabled is False
    assert repository.handoff_status == "waiting"
    assert len(repository.outbounds) == 1

    assert await engine.process(inbound(2, body="any later message")) is False
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_disabled_automation_does_not_change_state_or_create_outbound() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.MENU,
        context={"service_id": str(SERVICE_ID)},
        automation_enabled=False,
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action="menu.book")) is False

    assert repository.state == ConversationState.MENU
    assert repository.context == {"service_id": str(SERVICE_ID)}
    assert repository.outbounds == []


@mark.parametrize(
    ("state", "context", "action"),
    [
        (ConversationState.MENU, {}, "unknown.action"),
        (ConversationState.BOOKING_SERVICE, {}, "service:not-a-uuid"),
        (
            ConversationState.BOOKING_SERVICE,
            {},
            "service:50000000-0000-0000-0000-000000000005",
        ),
        (
            ConversationState.BOOKING_DATE,
            {"service_id": str(SERVICE_ID)},
            "date:invalid",
        ),
        (
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
            "time:invalid",
        ),
    ],
)
@mark.asyncio
async def test_unexpected_or_invalid_ids_do_not_advance_state(
    state: ConversationState,
    context: dict[str, Any],
    action: str,
) -> None:
    repository = FakeConversationRepository(state=state, context=context)
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == state
    assert repository.context == context
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_completed_conversation_returns_to_menu_on_new_message() -> None:
    repository = FakeConversationRepository(
        state=ConversationState.COMPLETED,
        context={"selected_time": "must-be-cleared"},
    )
    engine = ConversationEngine(repository)

    assert await engine.process(inbound(1, body="oi novamente")) is True

    assert repository.state == ConversationState.MENU
    assert repository.context == {}


@mark.parametrize(
    ("action", "expected_state", "expected_context"),
    [
        (
            "booking.back",
            ConversationState.BOOKING_TIME,
            {
                "service_id": str(SERVICE_ID),
                "selected_date": "2026-09-02",
            },
        ),
        ("booking.cancel", ConversationState.MENU, {}),
    ],
)
@mark.asyncio
async def test_booking_confirmation_navigation_uses_stable_ids(
    action: str,
    expected_state: ConversationState,
    expected_context: dict[str, Any],
) -> None:
    candidate = {
        "service_id": str(SERVICE_ID),
        "selected_date": "2026-09-02",
        "selected_time": "09:00",
    }
    repository = FakeConversationRepository(
        state=ConversationState.BOOKING_CONFIRM,
        context={**candidate, "candidate_booking": candidate},
    )
    engine = ConversationEngine(repository, FakeBookingPort())

    assert await engine.process(inbound(1, action=action)) is True

    assert repository.state == expected_state
    assert repository.context == expected_context


@mark.asyncio
async def test_same_inbound_is_idempotent_with_deterministic_unique_key() -> None:
    repository = FakeConversationRepository()
    engine = ConversationEngine(repository)
    message = inbound(1, body="hello")

    assert await engine.process(message) is True
    assert await engine.process(message) is False

    assert len(repository.outbounds) == 1
    stored = repository.outbounds[0]
    assert stored.idempotency_key == build_outbound_idempotency_key(message)
    assert stored.idempotency_key == build_outbound_idempotency_key(message)
    assert stored.status == "pending"
    assert stored.provider_message_id is None


@mark.asyncio
async def test_concurrent_same_conversation_is_serialized_and_idempotent() -> None:
    repository = FakeConversationRepository()
    engine = ConversationEngine(repository)
    message = inbound(1, body="concurrent")

    results = await asyncio.gather(
        engine.process(message),
        engine.process(message),
    )

    assert sorted(results) == [False, True]
    assert repository.state == ConversationState.MENU
    assert len(repository.outbounds) == 1


@mark.asyncio
async def test_engine_never_calls_whatsapp_client(
    monkeypatch: MonkeyPatch,
) -> None:
    client_methods = (
        "send_text",
        "send_interactive_buttons",
        "send_interactive_list",
        "mark_as_read",
    )
    mocks: list[AsyncMock] = []
    for method_name in client_methods:
        method_mock = AsyncMock(side_effect=AssertionError("Meta call is forbidden"))
        monkeypatch.setattr(WhatsAppClient, method_name, method_mock)
        mocks.append(method_mock)

    repository = FakeConversationRepository()
    assert await ConversationEngine(repository).process(
        inbound(1, body="hello")
    ) is True

    for method_mock in mocks:
        method_mock.assert_not_awaited()


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.inbounds: list[InboundMessageEvent] = []
        self.completed: list[tuple[str, str]] = []

    async def claim_event(self, event: InboundMessageEvent) -> bool:
        if event.event_key in self.claimed:
            return False
        self.claimed.add(event.event_key)
        return True

    async def find_business_id(self, _: str) -> uuid.UUID:
        return BUSINESS_ID

    async def get_or_create_customer_id(self, _: uuid.UUID, __: str) -> uuid.UUID:
        return CUSTOMER_ID

    async def get_or_create_conversation_id(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
    ) -> uuid.UUID:
        return CONVERSATION_ID

    async def touch_conversation(self, _: uuid.UUID) -> None:
        return None

    async def persist_inbound_message(
        self,
        _: uuid.UUID,
        __: uuid.UUID,
        event: InboundMessageEvent,
    ) -> None:
        self.inbounds.append(event)

    async def update_message_status(self, *_: Any) -> None:
        return None

    async def complete_event(self, event_key: str, event_status: str) -> None:
        self.completed.append((event_key, event_status))

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "claimed": self.claimed,
                "inbounds": self.inbounds,
                "completed": self.completed,
            }
        )

    def restore_state(self, state: dict[str, Any]) -> None:
        self.claimed = state["claimed"]
        self.inbounds = state["inbounds"]
        self.completed = state["completed"]


class FakeTransactionSession:
    def __init__(self, *stores: Any) -> None:
        self.stores = stores

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        snapshots = [store.export_state() for store in self.stores]
        try:
            yield
        except Exception:
            for store, snapshot in zip(self.stores, snapshots, strict=True):
                store.restore_state(snapshot)
            raise


def webhook_event() -> InboundMessageEvent:
    provider_message_id = "provider-webhook-engine"
    return InboundMessageEvent(
        event_key=build_event_key("inbound", provider_message_id),
        event_type="message.inbound.text",
        meta_phone_number_id="known-phone-id",
        provider_message_id=provider_message_id,
        whatsapp_id="customer-id",
        message_type="text",
        body="hello",
        interactive_id=None,
    )


@mark.asyncio
async def test_webhook_persists_inbound_and_outbox_in_same_transaction() -> None:
    event_repository = FakeWebhookRepository()
    conversation_repository = FakeConversationRepository()
    session = FakeTransactionSession(event_repository, conversation_repository)
    engine = ConversationEngine(conversation_repository)
    event = webhook_event()

    await process_webhook_events(
        session,
        [event],
        event_repository,
        engine,
    )

    assert event_repository.inbounds == [event]
    assert conversation_repository.state == ConversationState.MENU
    assert len(conversation_repository.outbounds) == 1
    assert event_repository.completed == [(event.event_key, "processed")]


@mark.asyncio
async def test_transaction_rollback_keeps_state_and_outbox_consistent() -> None:
    event_repository = FakeWebhookRepository()
    conversation_repository = FakeConversationRepository()
    conversation_repository.fail_after_outbound = True
    session = FakeTransactionSession(event_repository, conversation_repository)
    engine = ConversationEngine(conversation_repository)

    with raises(RuntimeError, match="simulated persistence failure"):
        await process_webhook_events(
            session,
            [webhook_event()],
            event_repository,
            engine,
        )

    assert event_repository.claimed == set()
    assert event_repository.inbounds == []
    assert event_repository.completed == []
    assert conversation_repository.state == ConversationState.START
    assert conversation_repository.outbounds == []
    assert conversation_repository.idempotency_keys == set()

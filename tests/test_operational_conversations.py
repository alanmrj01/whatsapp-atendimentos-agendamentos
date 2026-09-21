from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import Message
from app.operations import service as operations_service
from app.operations.schemas import (
    AutomationSettingsUpdate,
    EmployeeCreate,
    EmployeeUpdate,
    ManualMessageCreate,
)
from app.operations.service import OperationalService, _display_name


class RowResult:
    def __init__(self, row):  # type: ignore[no-untyped-def]
        self.row = row

    def one_or_none(self):  # type: ignore[no-untyped-def]
        return self.row


class FakeManualSession:
    def __init__(self, *, scalars, row):  # type: ignore[no-untyped-def]
        self.scalars = iter(scalars)
        self.row = row
        self.added = None
        self.events: list[str] = []
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, _statement):  # type: ignore[no-untyped-def]
        return next(self.scalars)

    async def execute(self, _statement):  # type: ignore[no-untyped-def]
        return RowResult(self.row)

    def add(self, item) -> None:  # type: ignore[no-untyped-def]
        self.events.append("message_added")
        if item.id is None:
            item.id = uuid4()
        if item.created_at is None:
            item.created_at = datetime.now(UTC)
        self.added = item

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class RecordingPolicy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple] = []

    async def register_manual_business_message(self, *args):  # type: ignore[no-untyped-def]
        self.events.append("human_control")
        self.calls.append(args)


def conversation_row(business_id, conversation_id):  # type: ignore[no-untyped-def]
    return (
        SimpleNamespace(id=conversation_id, business_id=business_id),
        SimpleNamespace(id=uuid4()),
        SimpleNamespace(id=business_id, meta_phone_number_id="legacy-pilot-phone"),
    )


@pytest.mark.asyncio
async def test_manual_message_is_idempotent_and_activates_human_control_first(
    monkeypatch,
) -> None:
    business_id, conversation_id, operation_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    session = FakeManualSession(
        scalars=[None, None, now],
        row=conversation_row(business_id, conversation_id),
    )
    policy = RecordingPolicy(session.events)
    monkeypatch.setattr(
        operations_service,
        "AutomationPolicyService",
        lambda _repository: policy,
    )

    result = await OperationalService(session).send_manual_message(
        business_id,
        conversation_id,
        ManualMessageCreate(text="  Retorno da equipe  "),
        operation_id,
    )

    assert result.status == "pending"
    assert result.body == "Retorno da equipe"
    assert session.added.idempotency_key == (
        f"manual:outbound:{business_id}:{conversation_id}:{operation_id}"
    )
    assert session.events == ["human_control", "message_added"]
    assert policy.calls[0][:2] == (business_id, conversation_id)
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_manual_message_replay_returns_existing_outbound_without_duplicate() -> None:
    business_id, conversation_id, operation_id = uuid4(), uuid4(), uuid4()
    existing = Message(
        id=uuid4(),
        business_id=business_id,
        conversation_id=conversation_id,
        direction="outbound",
        message_type="text",
        body="Já registrada",
        status="pending",
        idempotency_key=(
            f"manual:outbound:{business_id}:{conversation_id}:{operation_id}"
        ),
        created_at=datetime.now(UTC),
    )
    session = FakeManualSession(scalars=[existing], row=None)

    result = await OperationalService(session).send_manual_message(
        business_id,
        conversation_id,
        ManualMessageCreate(text="Já registrada"),
        operation_id,
    )

    assert result.id == existing.id
    assert session.added is None
    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_manual_message_is_blocked_outside_customer_service_window(
    monkeypatch,
) -> None:
    business_id, conversation_id = uuid4(), uuid4()
    session = FakeManualSession(
        scalars=[None, None, datetime.now(UTC) - timedelta(hours=24, seconds=1)],
        row=conversation_row(business_id, conversation_id),
    )
    policy = RecordingPolicy(session.events)
    monkeypatch.setattr(
        operations_service,
        "AutomationPolicyService",
        lambda _repository: policy,
    )

    with pytest.raises(HTTPException) as caught:
        await OperationalService(session).send_manual_message(
            business_id,
            conversation_id,
            ManualMessageCreate(text="Fora da janela"),
            uuid4(),
        )

    assert caught.value.status_code == 409
    assert "approved template" in caught.value.detail
    assert session.added is None
    assert policy.calls == []


@pytest.mark.asyncio
async def test_manual_message_for_conversation_outside_tenant_is_not_found() -> None:
    session = FakeManualSession(scalars=[None], row=None)

    with pytest.raises(HTTPException) as caught:
        await OperationalService(session).send_manual_message(
            uuid4(),
            uuid4(),
            ManualMessageCreate(text="Cross tenant"),
            uuid4(),
        )

    assert caught.value.status_code == 404
    assert session.added is None


def test_customer_name_precedence_preserves_manual_override() -> None:
    assert _display_name("Nome manual", "Nome WhatsApp", "+5511", "5511") == (
        "Nome manual"
    )
    assert _display_name(None, "Nome WhatsApp", "+5511", "5511") == (
        "Nome WhatsApp"
    )
    assert _display_name(None, None, "+5511", "5511") == "+5511"
    assert _display_name(None, None, None, "5511") == "5511"


def test_employee_operational_role_defaults_and_validation_are_domain_only() -> None:
    assert EmployeeCreate(name="Técnico").operational_role == "technician"
    for role in ("technician", "assistant", "administrator"):
        assert EmployeeCreate(name="Profissional", operational_role=role).operational_role == role
    with pytest.raises(ValidationError):
        EmployeeCreate(name="Profissional", operational_role="owner")
    with pytest.raises(ValidationError):
        EmployeeUpdate(operational_role=None)


def test_assistant_messages_are_bounded_normalized_plain_text() -> None:
    values = AutomationSettingsUpdate(
        assistant_enabled=False,
        greeting_message="  Olá!   Como posso ajudar?  ",
    )
    assert values.assistant_enabled is False
    assert values.greeting_message == "Olá! Como posso ajudar?"

    with pytest.raises(ValidationError):
        AutomationSettingsUpdate(greeting_message="<strong>Olá</strong>")
    with pytest.raises(ValidationError):
        AutomationSettingsUpdate(fallback_message="x" * 1001)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import ValidationError

from app.api.operational_pwa import (
    _authorize,
    _membership,
    get_operational_service,
    list_conversations,
    setup_status,
    update_automation,
    update_employee,
)
from app.auth.dependencies import require_origin, require_principal
from app.auth.schemas import MembershipResponse, MembershipRole
from app.main import app
from app.operations.schemas import (
    AppointmentCreate,
    AppointmentUpdate,
    DashboardMetrics,
    DashboardToday,
    EmployeeUpdate,
    SetupStatus,
    AutomationSettingsUpdate,
    AutomationSettingsView,
)

BUSINESS_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BUSINESS_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def principal(
    business_id: UUID = BUSINESS_A,
    *,
    role: MembershipRole = MembershipRole.OWNER,
    access_mode: str = "paid",
):
    membership = MembershipResponse(
        business_id=business_id,
        business_name="Empresa autorizada",
        role=role,
        access_mode=access_mode,
    )
    return SimpleNamespace(active_membership=lambda: membership)


class FakeOperationalService:
    def __init__(self) -> None:
        self.dashboard_business_id = None
        self.appointment_business_id = None
        self.dashboard_today = AsyncMock(side_effect=self._dashboard)
        self.get_appointment = AsyncMock(side_effect=self._get_appointment)
        self.create_appointment = AsyncMock()
        self.list_conversations = AsyncMock(return_value=([], 0))
        self.setup_status = AsyncMock(return_value=SetupStatus(
            company=True,
            business_hours=False,
            automation=True,
            agenda=False,
            whatsapp=False,
            completed=2,
            next_step="business_hours",
        ))
        self.update_automation = AsyncMock(
            return_value=AutomationSettingsView(human_control_window_minutes=60)
        )
        self.update_employee = AsyncMock()

    async def _dashboard(self, business_id):
        self.dashboard_business_id = business_id
        return DashboardToday(
            metrics=DashboardMetrics(
                waiting_count=2,
                in_progress_count=1,
                appointments_today_count=3,
                completed_today_count=1,
            ),
            upcoming_appointments=[],
        )

    async def _get_appointment(self, business_id, appointment_id):
        self.appointment_business_id = business_id
        raise HTTPException(404, "Appointment not found")


@pytest.mark.asyncio
async def test_dashboard_uses_active_membership_business_not_client_input(client: AsyncClient) -> None:
    fake = FakeOperationalService()
    app.dependency_overrides[require_principal] = lambda: principal(BUSINESS_A)
    app.dependency_overrides[get_operational_service] = lambda: fake
    try:
        response = await client.get("/api/v1/dashboard/today")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["metrics"]["waiting_count"] == 2
    assert fake.dashboard_business_id == BUSINESS_A


@pytest.mark.asyncio
async def test_business_a_cannot_select_business_b_through_query_or_foreign_id(client: AsyncClient) -> None:
    fake = FakeOperationalService()
    app.dependency_overrides[require_principal] = lambda: principal(BUSINESS_A)
    app.dependency_overrides[get_operational_service] = lambda: fake
    try:
        response = await client.get(
            f"/api/v1/appointments/{uuid4()}",
            params={"business_id": str(BUSINESS_B)},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert fake.appointment_business_id == BUSINESS_A
    assert fake.appointment_business_id != BUSINESS_B


@pytest.mark.asyncio
async def test_viewer_cannot_create_appointment(client: AsyncClient) -> None:
    fake = FakeOperationalService()
    fake.create_appointment.return_value = None
    app.dependency_overrides[require_principal] = lambda: principal(role=MembershipRole.VIEWER)
    app.dependency_overrides[require_origin] = lambda: None
    app.dependency_overrides[get_operational_service] = lambda: fake
    now = datetime.now(UTC)
    try:
        response = await client.post(
            "/api/v1/appointments",
            json={
                "customer_id": str(uuid4()),
                "service_id": str(uuid4()),
                "employee_id": str(uuid4()),
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    fake.create_appointment.assert_not_awaited()


def test_free_membership_is_fail_closed_for_operational_api() -> None:
    with pytest.raises(HTTPException) as caught:
        _membership(principal(access_mode="free"))
    assert caught.value.status_code == 402


def test_attendant_can_operate_agenda_but_not_configuration() -> None:
    value = principal(role=MembershipRole.ATTENDANT)
    assert _authorize(value, {MembershipRole.ATTENDANT}).business_id == BUSINESS_A
    with pytest.raises(HTTPException) as caught:
        _authorize(value, {MembershipRole.OWNER, MembershipRole.ADMIN})
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_conversation_filters_and_setup_are_scoped_to_active_business() -> None:
    fake = FakeOperationalService()
    identity = principal(BUSINESS_A)
    result = await list_conversations(
        identity,
        fake,
        search="cliente",
        conversation_status="waiting",
        page=2,
        page_size=10,
    )
    setup = await setup_status(identity, fake)
    assert result.total == 0
    assert setup.next_step == "business_hours"
    fake.list_conversations.assert_awaited_once_with(
        BUSINESS_A,
        search="cliente",
        status="waiting",
        page=2,
        page_size=10,
    )
    fake.setup_status.assert_awaited_once_with(BUSINESS_A)


@pytest.mark.asyncio
async def test_configuration_mutations_use_active_business_and_owner_role() -> None:
    fake = FakeOperationalService()
    identity = principal(BUSINESS_A)
    result = await update_automation(
        AutomationSettingsUpdate(human_control_window_minutes=60), identity, fake
    )
    assert result.human_control_window_minutes == 60
    fake.update_automation.assert_awaited_once_with(BUSINESS_A, 60)

    foreign_employee_id = uuid4()
    await update_employee(
        foreign_employee_id,
        EmployeeUpdate(active=False),
        identity,
        fake,
    )
    fake.update_employee.assert_awaited_once_with(
        BUSINESS_A, foreign_employee_id, EmployeeUpdate(active=False)
    )


@pytest.mark.asyncio
async def test_viewer_cannot_change_automation() -> None:
    fake = FakeOperationalService()
    with pytest.raises(HTTPException) as caught:
        await update_automation(
            AutomationSettingsUpdate(human_control_window_minutes=60),
            principal(role=MembershipRole.VIEWER),
            fake,
        )
    assert caught.value.status_code == 403
    fake.update_automation.assert_not_awaited()


def test_appointment_contract_requires_timezone_and_valid_interval() -> None:
    values = {
        "customer_id": uuid4(),
        "service_id": uuid4(),
        "employee_id": uuid4(),
        "starts_at": datetime(2026, 9, 10, 9),
        "ends_at": datetime(2026, 9, 10, 10),
    }
    with pytest.raises(ValidationError):
        AppointmentCreate(**values)
    values["starts_at"] = datetime(2026, 9, 10, 11, tzinfo=UTC)
    values["ends_at"] = datetime(2026, 9, 10, 10, tzinfo=UTC)
    with pytest.raises(ValidationError):
        AppointmentCreate(**values)


def test_appointment_update_supports_status_cancel_and_rejects_empty_payload() -> None:
    assert AppointmentUpdate(status="cancelled").status == "cancelled"
    with pytest.raises(ValidationError):
        AppointmentUpdate()


def test_operational_migration_is_additive_and_reversible() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "alembic/versions/20260908_0008_operational_appointments.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "20260908_0008"' in source
    assert 'down_revision = "20260904_0007"' in source
    assert 'op.add_column("appointments"' in source
    assert "'pending', 'confirmed', 'cancelled', 'completed'" in source
    assert 'op.drop_column("appointments", "notes")' in source

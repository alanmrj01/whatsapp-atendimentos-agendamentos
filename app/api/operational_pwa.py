from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_principal
from app.auth.schemas import MembershipResponse, MembershipRole
from app.auth.service import Principal
from app.core.database import get_db
from app.operations.schemas import (
    AppointmentCreate,
    AppointmentList,
    AppointmentUpdate,
    AppointmentView,
    AutomationSettingsUpdate,
    AutomationSettingsView,
    BusinessUpdate,
    BusinessView,
    ConversationDetail,
    ConversationList,
    CustomerCreate,
    CustomerOption,
    CustomerList,
    DashboardToday,
    EmployeeCreate,
    EmployeeList,
    EmployeeUpdate,
    EmployeeServicesUpdate,
    EmployeeView,
    ServiceList,
    ServiceCreate,
    ServiceOption,
    ServiceUpdate,
    SetupStatus,
    WorkingHoursCreate,
    WorkingHoursList,
    WorkingHoursUpdate,
    WorkingHoursView,
)
from app.operations.service import OperationalService

router = APIRouter(prefix="/api/v1", tags=["pwa-operations"])
Db = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[Principal, Depends(require_principal)]
ConversationFilter = Literal["waiting", "in_progress", "answered"]

CONFIG_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN}
AGENDA_ROLES = {*CONFIG_ROLES, MembershipRole.ATTENDANT}


def _membership(principal: Principal) -> MembershipResponse:
    membership = principal.active_membership()
    if membership.access_mode != "paid":
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Paid plan required")
    return membership


def _authorize(principal: Principal, roles: set[MembershipRole]) -> MembershipResponse:
    membership = _membership(principal)
    if membership.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only access")
    return membership


def get_operational_service(db: Db) -> OperationalService:
    return OperationalService(db)


ServiceDep = Annotated[OperationalService, Depends(get_operational_service)]


@router.get("/dashboard/today", response_model=DashboardToday)
async def dashboard_today(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.dashboard_today(membership.business_id)


@router.get("/appointments", response_model=AppointmentList)
async def list_appointments(
    principal: Identity,
    service: ServiceDep,
    selected_date: date | None = Query(default=None, alias="date"),
    starts_at: datetime | None = None,
    ends_before: datetime | None = None,
):
    membership = _membership(principal)
    for value in (starts_at, ends_before):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, "Appointment range must include timezone")
    if selected_date is not None and (starts_at is not None or ends_before is not None):
        raise HTTPException(422, "Use either date or timestamp range")
    if starts_at is not None and ends_before is not None:
        if ends_before <= starts_at:
            raise HTTPException(422, "Appointment range is invalid")
        if ends_before - starts_at > timedelta(days=366):
            raise HTTPException(422, "Appointment range is too large")
    return AppointmentList(items=await service.list_appointments(
        membership.business_id,
        selected_date=selected_date,
        starts_at=starts_at,
        ends_before=ends_before,
    ))


@router.post(
    "/appointments",
    response_model=AppointmentView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_appointment(
    payload: AppointmentCreate, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.create_appointment(membership.business_id, payload)


@router.get("/appointments/{appointment_id}", response_model=AppointmentView)
async def get_appointment(appointment_id: UUID, principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_appointment(membership.business_id, appointment_id)


@router.patch(
    "/appointments/{appointment_id}",
    response_model=AppointmentView,
    dependencies=[Depends(require_origin)],
)
async def update_appointment(
    appointment_id: UUID,
    payload: AppointmentUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.update_appointment(membership.business_id, appointment_id, payload)


@router.post(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentView,
    dependencies=[Depends(require_origin)],
)
async def cancel_appointment(appointment_id: UUID, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.update_appointment(
        membership.business_id, appointment_id, AppointmentUpdate(status="cancelled")
    )


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    principal: Identity,
    service: ServiceDep,
    search: str | None = Query(default=None, min_length=1, max_length=100),
    conversation_status: ConversationFilter | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=50),
):
    membership = _membership(principal)
    items, total = await service.list_conversations(
        membership.business_id,
        search=search,
        status=conversation_status,
        page=page,
        page_size=page_size,
    )
    return ConversationList(items=items, page=page, page_size=page_size, total=total)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: UUID, principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_conversation(membership.business_id, conversation_id)


@router.get("/business", response_model=BusinessView)
async def get_business(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_business(membership.business_id)


@router.patch(
    "/business",
    response_model=BusinessView,
    dependencies=[Depends(require_origin)],
)
async def update_business(payload: BusinessUpdate, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_business(membership.business_id, payload)


@router.get("/working-hours", response_model=WorkingHoursList)
async def list_working_hours(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return WorkingHoursList(items=await service.list_working_hours(membership.business_id))


@router.post(
    "/working-hours",
    response_model=WorkingHoursView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_working_hours(payload: WorkingHoursCreate, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.create_working_hours(membership.business_id, payload)


@router.patch(
    "/working-hours/{hours_id}",
    response_model=WorkingHoursView,
    dependencies=[Depends(require_origin)],
)
async def update_working_hours(
    hours_id: UUID, payload: WorkingHoursUpdate, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_working_hours(membership.business_id, hours_id, payload)


@router.delete(
    "/working-hours/{hours_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_working_hours(hours_id: UUID, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    await service.delete_working_hours(membership.business_id, hours_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/automation", response_model=AutomationSettingsView)
async def get_automation(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_automation(membership.business_id)


@router.patch(
    "/automation",
    response_model=AutomationSettingsView,
    dependencies=[Depends(require_origin)],
)
async def update_automation(
    payload: AutomationSettingsUpdate, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_automation(
        membership.business_id, payload.human_control_window_minutes
    )


@router.get("/employees", response_model=EmployeeList)
async def list_employees(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return EmployeeList(items=await service.list_employees(membership.business_id))


@router.post(
    "/employees",
    response_model=EmployeeView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_employee(payload: EmployeeCreate, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.create_employee(membership.business_id, payload)


@router.patch(
    "/employees/{employee_id}",
    response_model=EmployeeView,
    dependencies=[Depends(require_origin)],
)
async def update_employee(
    employee_id: UUID, payload: EmployeeUpdate, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_employee(membership.business_id, employee_id, payload)


@router.put(
    "/employees/{employee_id}/services",
    response_model=EmployeeView,
    dependencies=[Depends(require_origin)],
)
async def update_employee_services(
    employee_id: UUID,
    payload: EmployeeServicesUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_employee_services(
        membership.business_id, employee_id, payload
    )


@router.get("/customers", response_model=CustomerList)
async def list_customers(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return CustomerList(items=await service.list_customers(membership.business_id))


@router.post(
    "/customers",
    response_model=CustomerOption,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_customer(payload: CustomerCreate, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.create_customer(membership.business_id, payload)


@router.get("/services", response_model=ServiceList)
async def list_services(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return ServiceList(items=await service.list_services(membership.business_id))


@router.post(
    "/services",
    response_model=ServiceOption,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_service(payload: ServiceCreate, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.create_service(membership.business_id, payload)


@router.patch(
    "/services/{service_id}",
    response_model=ServiceOption,
    dependencies=[Depends(require_origin)],
)
async def update_service(
    service_id: UUID, payload: ServiceUpdate, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_service(membership.business_id, service_id, payload)


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.setup_status(membership.business_id)

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_principal
from app.auth.schemas import MembershipResponse, MembershipRole
from app.auth.service import Principal
from app.core.database import get_db
from app.core.config import CloudTasksConfigurationError, Settings, get_settings
from app.operations.address_lookup import PostalAddressLookupError, lookup_postal_address
from app.operations.schemas import (
    AppointmentCreate,
    AppointmentList,
    AppointmentUpdate,
    AppointmentView,
    AutomationExclusionList,
    AutomationExclusionView,
    AutomationSettingsUpdate,
    AutomationSettingsView,
    BusinessHoursUpdate,
    BusinessHoursView,
    BusinessUpdate,
    BusinessView,
    PostalAddressView,
    CatalogItemCreate,
    CatalogItemList,
    CatalogItemUpdate,
    CatalogItemView,
    ConversationDetail,
    ConversationActionUpdate,
    ConversationAutomationUpdate,
    ConversationBulkAction,
    ConversationBulkResult,
    ConversationList,
    ConversationMessageDelta,
    CustomerCreate,
    CustomerNameUpdate,
    CustomerOutreachList,
    CustomerOption,
    CustomerList,
    DashboardToday,
    EmployeeCreate,
    EmployeeList,
    EmployeeUpdate,
    EmployeeServicesUpdate,
    EmployeeView,
    ManualMessageCreate,
    MessageView,
    NotificationList,
    NotificationView,
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
from app.models import Message
from app.operations.service import OperationalService
from app.repositories.whatsapp_connections import WhatsAppConnectionRepository
from app.whatsapp.client import WhatsAppClientError
from app.whatsapp.sender import build_business_sender_resolver
from app.schemas.automation import AutomationExclusionCreate, AutomationExclusionUpdate
from app.tasks.cloud_tasks import CloudTasksEnqueueError
from app.tasks.outbound import (
    build_outbound_task_enqueuer,
    enqueue_outbound_message_ids,
)

router = APIRouter(prefix="/api/v1", tags=["pwa-operations"])
Db = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[Principal, Depends(require_principal)]
ConversationFilter = Literal["waiting", "in_progress", "answered"]

CONFIG_ROLES = {MembershipRole.OWNER, MembershipRole.ADMIN}
AGENDA_ROLES = {*CONFIG_ROLES, MembershipRole.ATTENDANT}


def _membership(principal: Principal) -> MembershipResponse:
    membership = principal.active_membership()
    # A former paid/admin-granted tenant keeps read access to its own operational
    # data. Only never-activated free tenants are demo-only and blocked here.
    if (
        membership.access_mode != "paid"
        and not membership.has_had_operational_access
    ):
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Subscription required")
    return membership


def _authorize(principal: Principal, roles: set[MembershipRole]) -> MembershipResponse:
    membership = _membership(principal)
    # Operational history grants read-only access, never mutation rights.
    if membership.access_mode != "paid":
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Active subscription required")
    if membership.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only access")
    return membership


def get_operational_service(db: Db) -> OperationalService:
    return OperationalService(db)


ServiceDep = Annotated[OperationalService, Depends(get_operational_service)]
Config = Annotated[Settings, Depends(get_settings)]


@router.get("/dashboard/today", response_model=DashboardToday)
async def dashboard_today(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.dashboard_today(membership.business_id)


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    principal: Identity,
    service: ServiceDep,
    unread_only: bool = False,
):
    membership = _membership(principal)
    return NotificationList(
        items=await service.list_notifications(
            membership.business_id,
            unread_only=unread_only,
        )
    )


@router.get("/customer-outreach", response_model=CustomerOutreachList)
async def list_customer_outreach(
    principal: Identity,
    service: ServiceDep,
    outreach_type: Literal["incomplete_24h", "cleaning_6m"] | None = None,
    outreach_status: Literal[
        "pending",
        "sent",
        "skipped",
        "responded",
        "accepted",
        "declined",
        "failed",
    ] | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
):
    membership = _membership(principal)
    return CustomerOutreachList(
        items=await service.list_customer_outreach(
            membership.business_id,
            outreach_type=outreach_type,
            status=outreach_status,
            limit=limit,
        )
    )


@router.patch(
    "/notifications/{notification_id}/read",
    response_model=NotificationView,
    dependencies=[Depends(require_origin)],
)
async def mark_notification_read(
    notification_id: UUID,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.mark_notification_read(
        membership.business_id,
        notification_id,
    )


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


@router.post(
    "/conversations/bulk",
    response_model=ConversationBulkResult,
    dependencies=[Depends(require_origin)],
)
async def bulk_conversation_action(
    payload: ConversationBulkAction,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.bulk_conversation_action(
        membership.business_id,
        payload,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessageDelta,
)
async def list_conversation_messages(
    conversation_id: UUID,
    principal: Identity,
    service: ServiceDep,
    after: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    membership = _membership(principal)
    if after is not None and after.tzinfo is None:
        raise HTTPException(422, "after must include timezone")
    return await service.list_conversation_messages(
        membership.business_id,
        conversation_id,
        after=after,
        limit=limit,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: UUID, principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_conversation(membership.business_id, conversation_id)


@router.get(
    "/conversations/{conversation_id}/messages/{message_id}/media",
)
async def get_conversation_message_media(
    conversation_id: UUID,
    message_id: UUID,
    request: Request,
    principal: Identity,
    db: Db,
):
    membership = _membership(principal)
    message = await db.scalar(
        select(Message).where(
            Message.business_id == membership.business_id,
            Message.conversation_id == conversation_id,
            Message.id == message_id,
        )
    )
    if message is None or message.media_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not found")
    if message.message_type not in {"image", "audio", "video"}:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Media preview is not available for this message type",
        )

    settings = get_settings()
    resolver = build_business_sender_resolver(
        WhatsAppConnectionRepository(db),
        settings,
    )
    sender = await resolver.resolve(membership.business_id)
    try:
        content, downloaded_mime_type = await sender.download_media(message.media_id)
    except WhatsAppClientError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Media is temporarily unavailable",
        ) from None
    finally:
        await sender.aclose()

    if not content:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Media is temporarily unavailable",
        )

    expected_prefix = f"{message.message_type}/"
    downloaded_mime = (
        downloaded_mime_type.strip()
        if isinstance(downloaded_mime_type, str)
        else ""
    )
    stored_mime = (
        message.media_mime_type.strip()
        if isinstance(message.media_mime_type, str)
        else ""
    )
    default_mime = {
        "image": "image/jpeg",
        "audio": "audio/mpeg",
        "video": "video/mp4",
    }[message.message_type]
    media_type = next(
        (
            candidate
            for candidate in (downloaded_mime, stored_mime)
            if candidate.casefold().startswith(expected_prefix)
        ),
        default_mime,
    )
    common_headers = {
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    range_header = request.headers.get("range")
    if range_header and message.message_type in {"audio", "video"}:
        range_value = range_header.strip().casefold()
        if not range_value.startswith("bytes=") or "," in range_value:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    **common_headers,
                    "Content-Range": f"bytes */{len(content)}",
                },
            )
        raw_range = range_value.removeprefix("bytes=")
        start_text, separator, end_text = raw_range.partition("-")
        try:
            if not separator:
                raise ValueError
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else len(content) - 1
            elif end_text:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    raise ValueError
                start = max(0, len(content) - suffix_length)
                end = len(content) - 1
            else:
                raise ValueError
        except ValueError:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    **common_headers,
                    "Content-Range": f"bytes */{len(content)}",
                },
            )

        if start < 0 or start >= len(content) or end < start:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    **common_headers,
                    "Content-Range": f"bytes */{len(content)}",
                },
            )
        end = min(end, len(content) - 1)
        partial = content[start : end + 1]
        return Response(
            content=partial,
            media_type=media_type,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers={
                **common_headers,
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
                "Content-Length": str(len(partial)),
            },
        )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            **common_headers,
            "Content-Length": str(len(content)),
        },
    )


@router.patch(
    "/conversations/{conversation_id}/customer",
    response_model=ConversationDetail,
    dependencies=[Depends(require_origin)],
)
async def update_conversation_customer(
    conversation_id: UUID,
    payload: CustomerNameUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.update_customer_name(
        membership.business_id, conversation_id, payload
    )


@router.patch(
    "/conversations/{conversation_id}/assistant",
    response_model=ConversationDetail,
    dependencies=[Depends(require_origin)],
)
async def update_conversation_assistant(
    conversation_id: UUID,
    payload: ConversationAutomationUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.update_conversation_automation(
        membership.business_id, conversation_id, payload
    )


@router.patch(
    "/conversations/{conversation_id}/actions",
    response_model=ConversationDetail,
    dependencies=[Depends(require_origin)],
)
async def update_conversation_actions(
    conversation_id: UUID,
    payload: ConversationActionUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    return await service.update_conversation_actions(
        membership.business_id, conversation_id, payload
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_conversation(
    conversation_id: UUID,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, AGENDA_ROLES)
    await service.delete_conversation(membership.business_id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def send_conversation_message(
    conversation_id: UUID,
    payload: ManualMessageCreate,
    principal: Identity,
    service: ServiceDep,
    settings: Config,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
):
    membership = _authorize(principal, AGENDA_ROLES)
    message = await service.send_manual_message(
        membership.business_id,
        conversation_id,
        payload,
        idempotency_key,
    )
    try:
        enqueuer = build_outbound_task_enqueuer(settings)
        await enqueue_outbound_message_ids([message.id], enqueuer)
    except (CloudTasksConfigurationError, CloudTasksEnqueueError):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Message delivery is temporarily unavailable",
        ) from None
    return message


@router.get("/address/cep/{postal_code}", response_model=PostalAddressView)
async def lookup_company_postal_code(postal_code: str, principal: Identity):
    _membership(principal)
    try:
        address = await lookup_postal_address(postal_code)
    except PostalAddressLookupError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return PostalAddressView(
        postal_code=address.postal_code,
        street=address.street,
        neighborhood=address.neighborhood,
        city=address.city,
        state=address.state,
    )


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


@router.get("/business-hours", response_model=BusinessHoursView)
async def get_business_hours(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.get_business_hours(membership.business_id)


@router.put(
    "/business-hours",
    response_model=BusinessHoursView,
    dependencies=[Depends(require_origin)],
)
async def update_business_hours(
    payload: BusinessHoursUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_business_hours(membership.business_id, payload)


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


@router.get("/automation/exclusions", response_model=AutomationExclusionList)
async def list_automation_exclusions(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return AutomationExclusionList(
        items=await service.list_automation_exclusions(membership.business_id)
    )


@router.post(
    "/automation/exclusions",
    response_model=AutomationExclusionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_automation_exclusion(
    payload: AutomationExclusionCreate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.create_automation_exclusion(membership.business_id, payload)


@router.patch(
    "/automation/exclusions/{exclusion_id}",
    response_model=AutomationExclusionView,
    dependencies=[Depends(require_origin)],
)
async def update_automation_exclusion(
    exclusion_id: UUID,
    payload: AutomationExclusionUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_automation_exclusion(
        membership.business_id, exclusion_id, payload
    )


@router.delete(
    "/automation/exclusions/{exclusion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_automation_exclusion(
    exclusion_id: UUID,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    await service.delete_automation_exclusion(membership.business_id, exclusion_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
        membership.business_id, payload
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


@router.delete(
    "/employees/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_employee(
    employee_id: UUID, principal: Identity, service: ServiceDep
):
    membership = _authorize(principal, CONFIG_ROLES)
    await service.delete_employee(membership.business_id, employee_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


@router.delete(
    "/services/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_service(
    service_id: UUID,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    await service.delete_service(membership.business_id, service_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return await service.setup_status(membership.business_id)


@router.post(
    "/setup/complete",
    response_model=SetupStatus,
    dependencies=[Depends(require_origin)],
)
async def complete_setup(principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.complete_onboarding(membership.business_id)


@router.get("/catalog-items", response_model=CatalogItemList)
async def list_catalog_items(principal: Identity, service: ServiceDep):
    membership = _membership(principal)
    return CatalogItemList(items=await service.list_catalog_items(membership.business_id))


@router.post(
    "/catalog-items",
    response_model=CatalogItemView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_origin)],
)
async def create_catalog_item(
    payload: CatalogItemCreate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.create_catalog_item(membership.business_id, payload)


@router.patch(
    "/catalog-items/{item_id}",
    response_model=CatalogItemView,
    dependencies=[Depends(require_origin)],
)
async def update_catalog_item(
    item_id: UUID,
    payload: CatalogItemUpdate,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    return await service.update_catalog_item(membership.business_id, item_id, payload)


@router.put(
    "/catalog-items/{item_id}/image",
    response_model=CatalogItemView,
    dependencies=[Depends(require_origin)],
)
async def upload_catalog_item_image(
    item_id: UUID,
    request: Request,
    principal: Identity,
    service: ServiceDep,
):
    membership = _authorize(principal, CONFIG_ROLES)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 4 * 1024 * 1024:
                raise HTTPException(413, "Catalog image exceeds 4 MB")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
    image_data = await request.body()
    public_url = (
        str(request.base_url).rstrip("/")
        + f"/api/v1/public/catalog-items/{item_id}/image"
    )
    return await service.set_catalog_item_image(
        membership.business_id,
        item_id,
        image_data=image_data,
        content_type=content_type,
        public_url=public_url,
    )


@router.get("/public/catalog-items/{item_id}/image")
async def public_catalog_item_image(item_id: UUID, service: ServiceDep):
    image_data, content_type = await service.get_catalog_item_image(item_id)
    return Response(
        content=image_data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.delete(
    "/catalog-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def delete_catalog_item(item_id: UUID, principal: Identity, service: ServiceDep):
    membership = _authorize(principal, CONFIG_ROLES)
    await service.delete_catalog_item(membership.business_id, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

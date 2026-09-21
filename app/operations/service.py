from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.automation.domain import ExclusionMode
from app.automation.service import (
    AutomationAdministrationService,
    AutomationPolicyService,
)
from app.models import (
    Appointment,
    Business,
    BusinessAutomationExclusion,
    BusinessWhatsAppConnection,
    Conversation,
    Customer,
    Employee,
    EmployeeService,
    Message,
    Service,
    WorkingHours,
)
from app.operations.schemas import (
    AppointmentCreate,
    AppointmentUpdate,
    AppointmentView,
    AssistantExclusionCreate,
    AssistantExclusionView,
    AutomationSettingsUpdate,
    AutomationSettingsView,
    BusinessUpdate,
    BusinessView,
    ConversationDetail,
    ConversationAutomationUpdate,
    ConversationPinUpdate,
    ConversationReadUpdate,
    ConversationView,
    CustomerNameUpdate,
    CustomerCreate,
    CustomerOption,
    DashboardMetrics,
    DashboardToday,
    EmployeeCreate,
    EmployeeServicesUpdate,
    EmployeeUpdate,
    EmployeeView,
    MessageView,
    ManualMessageCreate,
    ServiceOption,
    ServiceCreate,
    ServiceUpdate,
    SetupStatus,
    WorkingHoursCreate,
    WorkingHoursUpdate,
    WorkingHoursView,
)
from app.repositories.automation import AutomationRepository
from app.schemas.automation import (
    AutomationExclusionCreate,
    AutomationExclusionUpdate,
    BusinessAutomationSettingsUpdate,
)
from app.whatsapp.connections import WhatsAppConnectionStatus


class OperationalService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def dashboard_today(self, business_id: UUID) -> DashboardToday:
        business = await self._business(business_id)
        now = datetime.now(UTC)
        start, end = _day_bounds(now.astimezone(ZoneInfo(business.timezone)).date(), business.timezone)
        appointment_rows = await self.session.execute(
            self._appointment_query(business_id).where(
                Appointment.starts_at >= start,
                Appointment.starts_at < end,
            ).order_by(Appointment.starts_at)
        )
        appointments = [_appointment_view(row) for row in appointment_rows.all()]
        conversation_rows, _ = await self._conversation_rows(business_id)
        conversations = [_conversation_view(row) for row in conversation_rows]
        upcoming = [
            item for item in appointments
            if item.status != "cancelled" and item.starts_at >= now
        ][:5]
        return DashboardToday(
            metrics=DashboardMetrics(
                waiting_count=sum(item.status == "waiting" for item in conversations),
                in_progress_count=sum(item.status == "in_progress" for item in conversations),
                appointments_today_count=sum(item.status != "cancelled" for item in appointments),
                completed_today_count=sum(item.status == "completed" for item in appointments),
            ),
            upcoming_appointments=upcoming,
        )

    async def list_appointments(
        self,
        business_id: UUID,
        *,
        selected_date: date | None = None,
        starts_at: datetime | None = None,
        ends_before: datetime | None = None,
    ) -> list[AppointmentView]:
        query = self._appointment_query(business_id)
        if selected_date is None and starts_at is None and ends_before is None:
            business = await self._business(business_id)
            selected_date = datetime.now(UTC).astimezone(
                ZoneInfo(business.timezone)
            ).date()
        if selected_date is not None:
            business = await self._business(business_id)
            starts_at, ends_before = _day_bounds(selected_date, business.timezone)
        if starts_at is not None:
            query = query.where(Appointment.starts_at >= starts_at)
        if ends_before is not None:
            query = query.where(Appointment.starts_at < ends_before)
        rows = await self.session.execute(query.order_by(Appointment.starts_at, Appointment.id))
        return [_appointment_view(row) for row in rows.all()]

    async def get_appointment(self, business_id: UUID, appointment_id: UUID) -> AppointmentView:
        row = (await self.session.execute(
            self._appointment_query(business_id).where(Appointment.id == appointment_id)
        )).one_or_none()
        if row is None:
            raise HTTPException(404, "Appointment not found")
        return _appointment_view(row)

    async def create_appointment(self, business_id: UUID, values: AppointmentCreate) -> AppointmentView:
        business, customer, service, employee = await self._appointment_references(
            business_id, values.customer_id, values.service_id, values.employee_id
        )
        appointment = Appointment(
            business_id=business_id,
            customer_id=customer.id,
            service_id=service.id,
            employee_id=employee.id,
            starts_at=values.starts_at,
            ends_at=values.ends_at,
            status=values.status,
            notes=values.notes,
            quantity=1,
            access_condition="normal",
            estimated_duration_minutes=service.duration_minutes,
            travel_before_minutes=business.travel_before_buffer_minutes,
            travel_after_minutes=business.travel_after_buffer_minutes,
            estimated_price=service.base_price,
            pricing_type=service.pricing_type,
            estimate_details={"source": "operational_pwa"},
        )
        self.session.add(appointment)
        await self._commit_appointment()
        return await self.get_appointment(business_id, appointment.id)

    async def update_appointment(
        self, business_id: UUID, appointment_id: UUID, values: AppointmentUpdate
    ) -> AppointmentView:
        appointment = await self.session.scalar(
            select(Appointment).where(
                Appointment.business_id == business_id,
                Appointment.id == appointment_id,
            ).with_for_update()
        )
        if appointment is None:
            raise HTTPException(404, "Appointment not found")
        customer_id = values.customer_id or appointment.customer_id
        service_id = values.service_id or appointment.service_id
        employee_id = values.employee_id or appointment.employee_id
        _, _, service, _ = await self._appointment_references(
            business_id, customer_id, service_id, employee_id
        )
        updates = values.model_dump(exclude_unset=True)
        for field, value in updates.items():
            setattr(appointment, field, value)
        if appointment.ends_at <= appointment.starts_at:
            await self.session.rollback()
            raise HTTPException(422, "Appointment end must be after start")
        appointment.estimated_duration_minutes = service.duration_minutes
        appointment.estimated_price = service.base_price
        appointment.pricing_type = service.pricing_type
        await self._commit_appointment()
        return await self.get_appointment(business_id, appointment.id)

    async def list_conversations(
        self,
        business_id: UUID,
        *,
        search: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ConversationView], int]:
        rows, total = await self._conversation_rows(
            business_id,
            search=search,
            status=status,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        items = [_conversation_view(row) for row in rows]
        return items, total

    async def get_conversation(self, business_id: UUID, conversation_id: UUID) -> ConversationDetail:
        rows, _ = await self._conversation_rows(
            business_id, conversation_id=conversation_id
        )
        if not rows:
            raise HTTPException(404, "Conversation not found")
        view = _conversation_view(rows[0])
        messages = (await self.session.scalars(
            select(Message).where(
                Message.business_id == business_id,
                Message.conversation_id == conversation_id,
            ).order_by(Message.created_at, Message.id)
        )).all()
        last_inbound_at = await self.session.scalar(
            select(func.max(Message.created_at)).where(
                Message.business_id == business_id,
                Message.conversation_id == conversation_id,
                Message.direction == "inbound",
            )
        )
        window_expires_at = (
            last_inbound_at + timedelta(hours=24)
            if last_inbound_at is not None
            else None
        )
        return ConversationDetail(
            **view.model_dump(),
            messages=[
                MessageView(
                    id=item.id,
                    direction=item.direction,
                    message_type=item.message_type,
                    body=item.body,
                    status=item.status,
                    created_at=item.created_at,
                ) for item in messages
            ],
            assistant_enabled=rows[0][0].automation_enabled,
            automation_suppressed_until=rows[0][0].automation_suppressed_until,
            free_form_window_open=(
                window_expires_at is not None and window_expires_at > datetime.now(UTC)
            ),
            free_form_window_expires_at=window_expires_at,
        )

    async def update_customer_name(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: CustomerNameUpdate,
    ) -> ConversationDetail:
        customer = await self.session.scalar(
            select(Customer)
            .join(
                Conversation,
                and_(
                    Conversation.business_id == Customer.business_id,
                    Conversation.customer_id == Customer.id,
                ),
            )
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
        )
        if customer is None:
            raise HTTPException(404, "Conversation not found")
        customer.name = values.name
        await self.session.commit()
        return await self.get_conversation(business_id, conversation_id)

    async def update_conversation_automation(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: ConversationAutomationUpdate,
    ) -> ConversationDetail:
        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        conversation.automation_enabled = values.enabled
        if values.enabled:
            conversation.handoff_status = "none"
            conversation.automation_suppressed_until = None
            conversation.suppression_reason = None
        else:
            conversation.handoff_status = "waiting"
            await AutomationRepository(self.session).cancel_pending_outbounds(
                business_id, conversation_id
            )
        await self.session.commit()
        return await self.get_conversation(business_id, conversation_id)

    async def set_conversation_pinned(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: ConversationPinUpdate,
    ) -> ConversationDetail:
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.archived_at.is_(None),
            ).with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        conversation.pinned_at = datetime.now(UTC) if values.pinned else None
        await self.session.commit()
        return await self.get_conversation(business_id, conversation_id)

    async def set_conversation_read(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: ConversationReadUpdate,
    ) -> ConversationDetail:
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.archived_at.is_(None),
            ).with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        if values.unread:
            conversation.manual_unread = True
        else:
            conversation.last_read_at = datetime.now(UTC)
            conversation.manual_unread = False
        await self.session.commit()
        return await self.get_conversation(business_id, conversation_id)

    async def archive_conversation(
        self,
        business_id: UUID,
        conversation_id: UUID,
    ) -> None:
        conversation = await self.session.scalar(
            select(Conversation).where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.archived_at.is_(None),
            ).with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        conversation.archived_at = datetime.now(UTC)
        await self.session.commit()

    async def list_assistant_exclusions(
        self,
        business_id: UUID,
    ) -> list[AssistantExclusionView]:
        rows = (
            await self.session.execute(
                select(
                    BusinessAutomationExclusion,
                    Customer.id,
                    Customer.name,
                    Customer.whatsapp_profile_name,
                    Customer.phone_e164,
                )
                .outerjoin(
                    Customer,
                    and_(
                        Customer.business_id == BusinessAutomationExclusion.business_id,
                        Customer.whatsapp_id == BusinessAutomationExclusion.whatsapp_id,
                    ),
                )
                .where(
                    BusinessAutomationExclusion.business_id == business_id,
                    BusinessAutomationExclusion.mode == ExclusionMode.HUMAN_ONLY.value,
                    BusinessAutomationExclusion.active.is_(True),
                )
                .order_by(BusinessAutomationExclusion.created_at)
            )
        ).all()
        return [
            AssistantExclusionView(
                id=exclusion.id,
                customer_id=customer_id,
                customer_name=_display_name(
                    customer_name,
                    profile_name,
                    customer_phone,
                    exclusion.whatsapp_id,
                ),
                customer_phone=customer_phone,
                reason=exclusion.reason,
                active=exclusion.active,
            )
            for exclusion, customer_id, customer_name, profile_name, customer_phone in rows
        ]

    async def add_assistant_exclusion(
        self,
        business_id: UUID,
        values: AssistantExclusionCreate,
    ) -> AssistantExclusionView:
        customer = await self.session.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.id == values.customer_id,
            )
        )
        if customer is None:
            raise HTTPException(404, "Customer not found")

        repository = AutomationRepository(self.session)
        existing = await self.session.scalar(
            select(BusinessAutomationExclusion).where(
                BusinessAutomationExclusion.business_id == business_id,
                BusinessAutomationExclusion.whatsapp_id == customer.whatsapp_id,
            )
        )
        administration = AutomationAdministrationService(repository)
        if existing is None:
            exclusion = await administration.add_exclusion(
                business_id,
                AutomationExclusionCreate(
                    whatsapp_id=customer.whatsapp_id,
                    mode=ExclusionMode.HUMAN_ONLY,
                    label=_customer_display_name(customer),
                    reason=values.reason,
                    active=True,
                ),
            )
        else:
            exclusion = await administration.update_exclusion(
                business_id,
                existing.id,
                AutomationExclusionUpdate(
                    mode=ExclusionMode.HUMAN_ONLY,
                    label=_customer_display_name(customer),
                    reason=values.reason,
                    active=True,
                ),
            )
            if exclusion is None:
                raise HTTPException(404, "Exclusion not found")
        await self.session.commit()
        return AssistantExclusionView(
            id=exclusion.id,
            customer_id=customer.id,
            customer_name=_customer_display_name(customer),
            customer_phone=customer.phone_e164,
            reason=exclusion.reason,
            active=exclusion.active,
        )

    async def remove_assistant_exclusion(
        self,
        business_id: UUID,
        exclusion_id: UUID,
    ) -> None:
        exclusion = await self.session.scalar(
            select(BusinessAutomationExclusion).where(
                BusinessAutomationExclusion.business_id == business_id,
                BusinessAutomationExclusion.id == exclusion_id,
                BusinessAutomationExclusion.mode == ExclusionMode.HUMAN_ONLY.value,
            )
        )
        if exclusion is None:
            raise HTTPException(404, "Exclusion not found")
        await AutomationRepository(self.session).delete_exclusion(
            business_id, exclusion_id
        )
        await self.session.commit()

    async def send_manual_message(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: ManualMessageCreate,
        operation_id: UUID,
    ) -> MessageView:
        idempotency_key = (
            f"manual:outbound:{business_id}:{conversation_id}:{operation_id}"
        )
        existing = await self.session.scalar(
            select(Message).where(Message.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return _message_view(existing)

        row = (
            await self.session.execute(
                select(Conversation, Customer, Business)
                .join(
                    Customer,
                    and_(
                        Customer.business_id == Conversation.business_id,
                        Customer.id == Conversation.customer_id,
                    ),
                )
                .join(Business, Business.id == Conversation.business_id)
                .where(
                    Conversation.business_id == business_id,
                    Conversation.id == conversation_id,
                )
                .with_for_update(of=Conversation)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Conversation not found")
        conversation, _customer, business = row

        connection = await self.session.scalar(
            select(BusinessWhatsAppConnection)
            .where(BusinessWhatsAppConnection.business_id == business_id)
            .order_by(BusinessWhatsAppConnection.created_at.desc())
            .limit(1)
        )
        if connection is not None:
            connected = connection.status == WhatsAppConnectionStatus.CONNECTED.value
        else:
            connected = bool(business.meta_phone_number_id)
        if not connected:
            raise HTTPException(409, "WhatsApp connection is unavailable")

        last_inbound_at = await self.session.scalar(
            select(func.max(Message.created_at)).where(
                Message.business_id == business_id,
                Message.conversation_id == conversation_id,
                Message.direction == "inbound",
            )
        )
        if (
            last_inbound_at is None
            or last_inbound_at + timedelta(hours=24) <= datetime.now(UTC)
        ):
            raise HTTPException(
                409,
                "Customer service window is closed; use an approved template",
            )

        occurred_at = datetime.now(UTC)
        await AutomationPolicyService(
            AutomationRepository(self.session)
        ).register_manual_business_message(
            business_id,
            conversation_id,
            occurred_at,
        )
        message = Message(
            business_id=business_id,
            conversation_id=conversation_id,
            provider_message_id=None,
            direction="outbound",
            message_type="text",
            body=values.text,
            interactive_id=None,
            outbound_payload=None,
            status="pending",
            idempotency_key=idempotency_key,
        )
        self.session.add(message)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            replay = await self.session.scalar(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
            if replay is not None:
                return _message_view(replay)
            raise
        return _message_view(message)

    async def get_business(self, business_id: UUID) -> BusinessView:
        return _business_view(await self._business(business_id))

    async def update_business(self, business_id: UUID, values: BusinessUpdate) -> BusinessView:
        business = await self._business(business_id, for_update=True)
        for field, value in values.model_dump(exclude_unset=True).items():
            setattr(business, field, value)
        await self.session.commit()
        return _business_view(business)

    async def list_working_hours(self, business_id: UUID) -> list[WorkingHoursView]:
        rows = await self.session.execute(
            select(WorkingHours, Employee.name)
            .join(Employee, and_(Employee.business_id == WorkingHours.business_id, Employee.id == WorkingHours.employee_id))
            .where(WorkingHours.business_id == business_id)
            .order_by(WorkingHours.weekday, WorkingHours.start_time, Employee.name)
        )
        return [_hours_view(row) for row in rows.all()]

    async def create_working_hours(self, business_id: UUID, values: WorkingHoursCreate) -> WorkingHoursView:
        employee = await self._employee(business_id, values.employee_id)
        item = WorkingHours(business_id=business_id, **values.model_dump())
        self.session.add(item)
        await self.session.commit()
        return WorkingHoursView(id=item.id, employee_name=employee.name, **values.model_dump())

    async def update_working_hours(
        self, business_id: UUID, hours_id: UUID, values: WorkingHoursUpdate
    ) -> WorkingHoursView:
        item = await self.session.scalar(select(WorkingHours).where(
            WorkingHours.business_id == business_id, WorkingHours.id == hours_id
        ).with_for_update())
        if item is None:
            raise HTTPException(404, "Working hours not found")
        employee_id = values.employee_id or item.employee_id
        employee = await self._employee(business_id, employee_id)
        for field, value in values.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        if item.end_time <= item.start_time:
            await self.session.rollback()
            raise HTTPException(422, "End time must be after start time")
        await self.session.commit()
        return WorkingHoursView(
            id=item.id, employee_id=item.employee_id, employee_name=employee.name,
            weekday=item.weekday, start_time=item.start_time, end_time=item.end_time,
        )

    async def delete_working_hours(self, business_id: UUID, hours_id: UUID) -> None:
        item = await self.session.scalar(select(WorkingHours).where(
            WorkingHours.business_id == business_id, WorkingHours.id == hours_id
        ).with_for_update())
        if item is None:
            raise HTTPException(404, "Working hours not found")
        await self.session.delete(item)
        await self.session.commit()

    async def get_automation(self, business_id: UUID) -> AutomationSettingsView:
        settings = await AutomationAdministrationService(
            AutomationRepository(self.session)
        ).get_effective_settings(business_id)
        if settings is None:
            raise HTTPException(404, "Business not found")
        return AutomationSettingsView(
            human_control_window_minutes=settings.human_control_window_minutes,
            assistant_enabled=settings.assistant_enabled,
            greeting_message=settings.greeting_message,
            fallback_message=settings.fallback_message,
            handoff_message=settings.handoff_message,
        )

    async def update_automation(
        self,
        business_id: UUID,
        values: AutomationSettingsUpdate,
    ) -> AutomationSettingsView:
        updated = await AutomationAdministrationService(
            AutomationRepository(self.session)
        ).update_settings(
            business_id,
            BusinessAutomationSettingsUpdate(**values.model_dump(exclude_unset=True)),
        )
        if not updated:
            raise HTTPException(404, "Business not found")
        await self.session.commit()
        return await self.get_automation(business_id)

    async def list_employees(self, business_id: UUID) -> list[EmployeeView]:
        items = (await self.session.scalars(
            select(Employee).where(Employee.business_id == business_id).order_by(Employee.active.desc(), Employee.name)
        )).all()
        links = (await self.session.execute(
            select(EmployeeService.employee_id, EmployeeService.service_id).where(
                EmployeeService.business_id == business_id
            )
        )).all()
        service_ids = {item.id: [] for item in items}
        for employee_id, service_id in links:
            service_ids.setdefault(employee_id, []).append(service_id)
        return [_employee_view(item, service_ids.get(item.id, [])) for item in items]

    async def create_employee(self, business_id: UUID, values: EmployeeCreate) -> EmployeeView:
        item = Employee(
            business_id=business_id,
            name=values.name,
            operational_role=values.operational_role,
            active=True,
        )
        self.session.add(item)
        await self.session.commit()
        return _employee_view(item, [])

    async def update_employee(self, business_id: UUID, employee_id: UUID, values: EmployeeUpdate) -> EmployeeView:
        item = await self._employee(business_id, employee_id, for_update=True)
        for field, value in values.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        await self.session.commit()
        service_ids = list((await self.session.scalars(
            select(EmployeeService.service_id).where(
                EmployeeService.business_id == business_id,
                EmployeeService.employee_id == employee_id,
            )
        )).all())
        return _employee_view(item, service_ids)

    async def update_employee_services(
        self, business_id: UUID, employee_id: UUID, values: EmployeeServicesUpdate
    ) -> EmployeeView:
        employee = await self._employee(business_id, employee_id, for_update=True)
        unique_ids = list(dict.fromkeys(values.service_ids))
        if unique_ids:
            valid_ids = set((await self.session.scalars(
                select(Service.id).where(
                    Service.business_id == business_id,
                    Service.id.in_(unique_ids),
                    Service.active.is_(True),
                )
            )).all())
            if valid_ids != set(unique_ids):
                raise HTTPException(404, "Service not found")
        await self.session.execute(delete(EmployeeService).where(
            EmployeeService.business_id == business_id,
            EmployeeService.employee_id == employee_id,
        ))
        self.session.add_all([
            EmployeeService(business_id=business_id, employee_id=employee_id, service_id=service_id)
            for service_id in unique_ids
        ])
        await self.session.commit()
        return _employee_view(employee, unique_ids)

    async def list_customers(self, business_id: UUID) -> list[CustomerOption]:
        items = (await self.session.scalars(
            select(Customer).where(Customer.business_id == business_id).order_by(Customer.name, Customer.id)
        )).all()
        return [
            CustomerOption(
                id=item.id,
                name=_customer_display_name(item),
                phone=item.phone_e164,
            )
            for item in items
        ]

    async def create_customer(self, business_id: UUID, values: CustomerCreate) -> CustomerOption:
        await self._business(business_id)
        item = Customer(
            business_id=business_id,
            whatsapp_id=values.phone[1:],
            phone_e164=values.phone,
            name=values.name,
        )
        self.session.add(item)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(409, "Customer already exists") from None
        return CustomerOption(id=item.id, name=item.name or "Cliente", phone=item.phone_e164)

    async def list_services(self, business_id: UUID) -> list[ServiceOption]:
        items = (await self.session.scalars(
            select(Service).where(Service.business_id == business_id).order_by(Service.active.desc(), Service.name)
        )).all()
        return [ServiceOption(id=item.id, name=item.name, duration_minutes=item.duration_minutes, active=item.active) for item in items]

    async def create_service(self, business_id: UUID, values: ServiceCreate) -> ServiceOption:
        await self._business(business_id)
        item = Service(
            business_id=business_id,
            name=values.name,
            duration_minutes=values.duration_minutes,
            base_price=None,
            pricing_type="human_quote",
            automatic_booking=False,
            active=True,
        )
        self.session.add(item)
        await self.session.commit()
        return _service_view(item)

    async def update_service(
        self, business_id: UUID, service_id: UUID, values: ServiceUpdate
    ) -> ServiceOption:
        item = await self.session.scalar(select(Service).where(
            Service.business_id == business_id, Service.id == service_id
        ).with_for_update())
        if item is None:
            raise HTTPException(404, "Service not found")
        for field, value in values.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        await self.session.commit()
        return _service_view(item)

    async def setup_status(self, business_id: UUID) -> SetupStatus:
        business = await self._business(business_id)
        hours = bool(await self.session.scalar(
            select(func.count()).select_from(WorkingHours).join(
                Employee, and_(Employee.business_id == WorkingHours.business_id, Employee.id == WorkingHours.employee_id)
            ).where(WorkingHours.business_id == business_id, Employee.active.is_(True))
        ))
        agenda = bool(await self.session.scalar(
            select(func.count()).select_from(EmployeeService)
            .join(Employee, and_(Employee.business_id == EmployeeService.business_id, Employee.id == EmployeeService.employee_id))
            .join(Service, and_(Service.business_id == EmployeeService.business_id, Service.id == EmployeeService.service_id))
            .join(WorkingHours, and_(WorkingHours.business_id == EmployeeService.business_id, WorkingHours.employee_id == EmployeeService.employee_id))
            .where(EmployeeService.business_id == business_id, Employee.active.is_(True), Service.active.is_(True))
        )) and hours and business.slot_interval_minutes > 0
        whatsapp = bool(await self.session.scalar(select(func.count()).select_from(BusinessWhatsAppConnection).where(
            BusinessWhatsAppConnection.business_id == business_id,
            BusinessWhatsAppConnection.status == "connected",
        )))
        values = {
            "company": bool(business.name.strip()),
            "business_hours": hours,
            "automation": business.human_control_window_minutes in {5,10,20,30,60,120,240,360,720,1440,2160},
            "agenda": agenda,
            "whatsapp": whatsapp,
        }
        completed = sum(values.values())
        next_step = next((key for key, ready in values.items() if not ready), "complete")
        return SetupStatus(**values, completed=completed, next_step=next_step)

    async def _business(self, business_id: UUID, *, for_update: bool = False) -> Business:
        query = select(Business).where(Business.id == business_id, Business.active.is_(True))
        if for_update:
            query = query.with_for_update()
        item = await self.session.scalar(query)
        if item is None:
            raise HTTPException(404, "Business not found")
        return item

    async def _employee(self, business_id: UUID, employee_id: UUID, *, for_update: bool = False) -> Employee:
        query = select(Employee).where(Employee.business_id == business_id, Employee.id == employee_id)
        if for_update:
            query = query.with_for_update()
        item = await self.session.scalar(query)
        if item is None:
            raise HTTPException(404, "Employee not found")
        return item

    async def _appointment_references(
        self, business_id: UUID, customer_id: UUID, service_id: UUID, employee_id: UUID
    ) -> tuple[Business, Customer, Service, Employee]:
        business = await self._business(business_id)
        customer = await self.session.scalar(select(Customer).where(Customer.business_id == business_id, Customer.id == customer_id))
        service = await self.session.scalar(select(Service).where(Service.business_id == business_id, Service.id == service_id, Service.active.is_(True)))
        employee = await self.session.scalar(select(Employee).where(Employee.business_id == business_id, Employee.id == employee_id, Employee.active.is_(True)))
        if customer is None or service is None or employee is None:
            raise HTTPException(404, "Appointment reference not found")
        eligible = await self.session.scalar(select(EmployeeService.employee_id).where(
            EmployeeService.business_id == business_id,
            EmployeeService.employee_id == employee_id,
            EmployeeService.service_id == service_id,
        ))
        if eligible is None:
            raise HTTPException(422, "Employee is not assigned to service")
        return business, customer, service, employee

    async def _commit_appointment(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(409, "Appointment conflicts with existing schedule") from None

    def _appointment_query(self, business_id: UUID):
        return (
            select(Appointment, Customer.name, Customer.phone_e164, Service.name, Employee.name)
            .join(Customer, and_(Customer.business_id == Appointment.business_id, Customer.id == Appointment.customer_id))
            .join(Service, and_(Service.business_id == Appointment.business_id, Service.id == Appointment.service_id))
            .join(Employee, and_(Employee.business_id == Appointment.business_id, Employee.id == Appointment.employee_id))
            .where(Appointment.business_id == business_id)
        )

    async def _conversation_rows(
        self,
        business_id: UUID,
        *,
        search: str | None = None,
        conversation_id: UUID | None = None,
        status: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[Any], int]:
        latest_body = select(Message.body).where(
            Message.business_id == Conversation.business_id,
            Message.conversation_id == Conversation.id,
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1).correlate(Conversation).scalar_subquery()
        latest_time = select(Message.created_at).where(
            Message.business_id == Conversation.business_id,
            Message.conversation_id == Conversation.id,
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1).correlate(Conversation).scalar_subquery()
        latest_direction = select(Message.direction).where(
            Message.business_id == Conversation.business_id,
            Message.conversation_id == Conversation.id,
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1).correlate(Conversation).scalar_subquery()
        unread_message = aliased(Message)
        unread = select(func.count()).select_from(unread_message).where(
            unread_message.business_id == Conversation.business_id,
            unread_message.conversation_id == Conversation.id,
            unread_message.direction == "inbound",
            or_(
                Conversation.last_read_at.is_(None),
                unread_message.created_at > Conversation.last_read_at,
            ),
        ).correlate(Conversation).scalar_subquery()
        query = select(
            Conversation, Customer.name, Customer.whatsapp_profile_name,
            Customer.phone_e164, Customer.whatsapp_id,
            latest_body.label("last_content"), latest_time.label("last_message_at"),
            latest_direction.label("last_direction"), unread.label("unread_count"),
        ).join(Customer, and_(Customer.business_id == Conversation.business_id, Customer.id == Conversation.customer_id)).where(
            Conversation.business_id == business_id,
            Conversation.archived_at.is_(None),
        )
        if conversation_id is not None:
            query = query.where(Conversation.id == conversation_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                or_(
                    Customer.name.ilike(pattern),
                    Customer.whatsapp_profile_name.ilike(pattern),
                    Customer.phone_e164.ilike(pattern),
                    Customer.whatsapp_id.ilike(pattern),
                    latest_body.ilike(pattern),
                )
            )
        if status == "waiting":
            query = query.where(latest_direction == "inbound")
        elif status == "in_progress":
            query = query.where(
                or_(latest_direction.is_(None), latest_direction != "inbound"),
                Conversation.handoff_status != "none",
            )
        elif status == "answered":
            query = query.where(
                or_(latest_direction.is_(None), latest_direction != "inbound"),
                Conversation.handoff_status == "none",
            )
        total = int(await self.session.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        ) or 0)
        query = query.order_by(
            Conversation.pinned_at.desc().nullslast(),
            Conversation.manual_unread.desc(),
            unread.desc(),
            latest_time.desc().nullslast(),
            Conversation.id,
        )
        if offset is not None:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        result = await self.session.execute(query)
        return list(result.all()), total


def _day_bounds(value: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(value, time.min, zone).astimezone(UTC)
    return start, (datetime.combine(value + timedelta(days=1), time.min, zone).astimezone(UTC))


def _appointment_view(row: Any) -> AppointmentView:
    item, customer_name, customer_phone, service_name, employee_name = row
    return AppointmentView(
        id=item.id, customer_id=item.customer_id, customer_name=customer_name or "Cliente",
        customer_phone=customer_phone, service_id=item.service_id, service_name=service_name,
        employee_id=item.employee_id, employee_name=employee_name, starts_at=item.starts_at,
        ends_at=item.ends_at, status=item.status, notes=item.notes,
    )


def _conversation_view(row: Any) -> ConversationView:
    (
        item,
        customer_name,
        whatsapp_profile_name,
        customer_phone,
        whatsapp_id,
        last_content,
        last_message_at,
        last_direction,
        unread_count,
    ) = row
    status = "waiting" if last_direction == "inbound" else (
        "in_progress" if item.handoff_status != "none" else "answered"
    )
    unread_value = max(int(unread_count or 0), 1 if item.manual_unread else 0)
    return ConversationView(
        id=item.id,
        customer_id=item.customer_id,
        customer_name=_display_name(
            customer_name, whatsapp_profile_name, customer_phone, whatsapp_id
        ),
        customer_phone=customer_phone, last_content=last_content, last_message_at=last_message_at,
        status=status, unread_count=unread_value,
        priority=item.pinned_at is not None or unread_value > 0 or item.handoff_status == "waiting",
        pinned=item.pinned_at is not None,
        assignee_name=None,
    )


def _business_view(item: Business) -> BusinessView:
    return BusinessView(
        id=item.id,
        name=item.name,
        timezone=item.timezone,
        slot_interval_minutes=item.slot_interval_minutes,
        service_origin_address=item.service_origin_address,
        default_travel_minutes=item.default_travel_minutes,
        travel_fallback_allowed=item.travel_fallback_allowed,
        travel_before_buffer_minutes=item.travel_before_buffer_minutes,
        travel_after_buffer_minutes=item.travel_after_buffer_minutes,
    )


def _hours_view(row: Any) -> WorkingHoursView:
    item, employee_name = row
    return WorkingHoursView(
        id=item.id, employee_id=item.employee_id, employee_name=employee_name,
        weekday=item.weekday, start_time=item.start_time, end_time=item.end_time,
    )


def _employee_view(item: Employee, service_ids: list[UUID]) -> EmployeeView:
    return EmployeeView(
        id=item.id,
        name=item.name,
        active=item.active,
        operational_role=item.operational_role,
        service_ids=service_ids,
    )


def _message_view(item: Message) -> MessageView:
    return MessageView(
        id=item.id,
        direction=item.direction,
        message_type=item.message_type,
        body=item.body,
        status=item.status,
        created_at=item.created_at,
    )


def _customer_display_name(item: Customer) -> str:
    return _display_name(
        item.name,
        item.whatsapp_profile_name,
        item.phone_e164,
        item.whatsapp_id,
    )


def _display_name(
    name: str | None,
    profile_name: str | None,
    phone: str | None,
    whatsapp_id: str,
) -> str:
    return name or profile_name or phone or whatsapp_id


def _service_view(item: Service) -> ServiceOption:
    return ServiceOption(
        id=item.id,
        name=item.name,
        duration_minutes=item.duration_minutes,
        active=item.active,
    )

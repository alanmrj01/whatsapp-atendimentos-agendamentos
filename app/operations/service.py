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

from app.automation.service import (
    AutomationAdministrationService,
    AutomationPolicyService,
)
from app.models import (
    Appointment,
    Business,
    BusinessCatalogItem,
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
    AutomationExclusionView,
    AutomationSettingsUpdate,
    AutomationSettingsView,
    BusinessUpdate,
    BusinessView,
    CatalogItemCreate,
    CatalogItemUpdate,
    CatalogItemView,
    ConversationDetail,
    ConversationActionUpdate,
    ConversationAutomationUpdate,
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
from app.conversations.service_semantics import (
    generate_service_intent_examples,
    normalize_service_text,
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


    async def update_conversation_actions(
        self,
        business_id: UUID,
        conversation_id: UUID,
        values: ConversationActionUpdate,
    ) -> ConversationDetail:
        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
        )
        if conversation is None or conversation.deleted_at is not None:
            raise HTTPException(404, "Conversation not found")
        updates = values.model_dump(exclude_unset=True)
        now = datetime.now(UTC)
        if "pinned" in updates:
            conversation.pinned_at = now if updates["pinned"] else None
        if "read" in updates:
            if updates["read"]:
                conversation.last_read_at = now
                conversation.manual_unread = False
            else:
                conversation.manual_unread = True
        await self.session.commit()
        return await self.get_conversation(business_id, conversation_id)

    async def delete_conversation(
        self,
        business_id: UUID,
        conversation_id: UUID,
    ) -> None:
        conversation = await self.session.scalar(
            select(Conversation)
            .where(
                Conversation.business_id == business_id,
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Conversation not found")
        conversation.deleted_at = datetime.now(UTC)
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


    async def list_automation_exclusions(
        self, business_id: UUID
    ) -> list[AutomationExclusionView]:
        items = await AutomationAdministrationService(
            AutomationRepository(self.session)
        ).list_exclusions(business_id)
        return [_automation_exclusion_view(item) for item in items]

    async def create_automation_exclusion(
        self,
        business_id: UUID,
        values: AutomationExclusionCreate,
    ) -> AutomationExclusionView:
        repository = AutomationRepository(self.session)
        item = await AutomationAdministrationService(repository).add_exclusion(
            business_id, values
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise HTTPException(409, "Contact is already configured") from None
        return _automation_exclusion_view(item)

    async def update_automation_exclusion(
        self,
        business_id: UUID,
        exclusion_id: UUID,
        values: AutomationExclusionUpdate,
    ) -> AutomationExclusionView:
        item = await AutomationAdministrationService(
            AutomationRepository(self.session)
        ).update_exclusion(business_id, exclusion_id, values)
        if item is None:
            raise HTTPException(404, "Automation exclusion not found")
        await self.session.commit()
        return _automation_exclusion_view(item)

    async def delete_automation_exclusion(
        self,
        business_id: UUID,
        exclusion_id: UUID,
    ) -> None:
        repository = AutomationRepository(self.session)
        item = await repository.get_exclusion(business_id, exclusion_id)
        if item is None:
            raise HTTPException(404, "Automation exclusion not found")
        await repository.delete_exclusion(business_id, exclusion_id)
        await self.session.commit()

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
        return [_service_view(item) for item in items]

    async def create_service(self, business_id: UUID, values: ServiceCreate) -> ServiceOption:
        await self._business(business_id)
        installation = "instala" in normalize_service_text(values.name)
        item = Service(
            business_id=business_id,
            name=values.name,
            duration_minutes=values.duration_minutes,
            base_price=values.price,
            pricing_type="fixed" if values.price is not None else "estimated",
            automatic_booking=True,
            requires_address=True,
            asks_tubing_length=installation,
            included_tubing_meters=3 if installation else None,
            intent_examples=list(generate_service_intent_examples(values.name)),
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
        updates = values.model_dump(exclude_unset=True)
        explicit_examples = "intent_examples" in updates
        if "price" in updates:
            item.base_price = updates.pop("price")
            item.pricing_type = "fixed" if item.base_price is not None else "estimated"
            item.automatic_booking = True
        if "name" in updates:
            installation = "instala" in normalize_service_text(updates["name"])
            if not explicit_examples:
                item.intent_examples = list(generate_service_intent_examples(updates["name"]))
            if installation and item.included_tubing_meters is None:
                item.asks_tubing_length = True
                item.included_tubing_meters = 3
        for field, value in updates.items():
            setattr(item, field, value)
        await self.session.commit()
        return _service_view(item)

    async def list_catalog_items(self, business_id: UUID) -> list[CatalogItemView]:
        await self._business(business_id)
        await self._ensure_catalog_presets(business_id)
        items = (await self.session.scalars(
            select(BusinessCatalogItem)
            .where(BusinessCatalogItem.business_id == business_id)
            .order_by(BusinessCatalogItem.active.desc(), BusinessCatalogItem.name)
        )).all()
        return [_catalog_item_view(item) for item in items]

    async def create_catalog_item(
        self, business_id: UUID, values: CatalogItemCreate
    ) -> CatalogItemView:
        await self._business(business_id)
        item = BusinessCatalogItem(
            business_id=business_id,
            kind=values.kind,
            name=values.name,
            description=values.description,
            price=values.price,
            unit_label=values.unit_label,
            active=True,
        )
        self.session.add(item)
        await self.session.commit()
        return _catalog_item_view(item)

    async def update_catalog_item(
        self, business_id: UUID, item_id: UUID, values: CatalogItemUpdate
    ) -> CatalogItemView:
        item = await self.session.scalar(
            select(BusinessCatalogItem).where(
                BusinessCatalogItem.business_id == business_id,
                BusinessCatalogItem.id == item_id,
            ).with_for_update()
        )
        if item is None:
            raise HTTPException(404, "Catalog item not found")
        for field, value in values.model_dump(exclude_unset=True).items():
            setattr(item, field, value)
        await self.session.commit()
        return _catalog_item_view(item)

    async def delete_catalog_item(self, business_id: UUID, item_id: UUID) -> None:
        item = await self.session.scalar(
            select(BusinessCatalogItem).where(
                BusinessCatalogItem.business_id == business_id,
                BusinessCatalogItem.id == item_id,
            ).with_for_update()
        )
        if item is None:
            raise HTTPException(404, "Catalog item not found")
        if item.preset_key:
            item.active = False
            item.price = None
        else:
            await self.session.delete(item)
        await self.session.commit()

    async def setup_status(self, business_id: UUID) -> SetupStatus:
        business = await self._business(business_id)
        await self._ensure_catalog_presets(business_id)

        active_technicians = int(await self.session.scalar(
            select(func.count()).select_from(Employee).where(
                Employee.business_id == business_id,
                Employee.active.is_(True),
                Employee.operational_role == "technician",
            )
        ) or 0)
        hours = bool(await self.session.scalar(
            select(func.count()).select_from(WorkingHours).join(
                Employee, and_(
                    Employee.business_id == WorkingHours.business_id,
                    Employee.id == WorkingHours.employee_id,
                )
            ).where(
                WorkingHours.business_id == business_id,
                Employee.active.is_(True),
                Employee.operational_role == "technician",
            )
        ))
        active_services = (await self.session.scalars(
            select(Service).where(
                Service.business_id == business_id,
                Service.active.is_(True),
            )
        )).all()
        active_materials = (await self.session.scalars(
            select(BusinessCatalogItem).where(
                BusinessCatalogItem.business_id == business_id,
                BusinessCatalogItem.active.is_(True),
            )
        )).all()
        whatsapp = bool(await self.session.scalar(
            select(func.count()).select_from(BusinessWhatsAppConnection).where(
                BusinessWhatsAppConnection.business_id == business_id,
                BusinessWhatsAppConnection.status == "connected",
            )
        ))

        company = bool(
            business.name.strip()
            and (business.responsible_name or "").strip()
            and (business.service_origin_address or "").strip()
        )
        team = active_technicians > 0
        services = bool(active_services) and all(item.base_price is not None for item in active_services)
        materials = bool(active_materials) and all(item.price is not None for item in active_materials)
        agenda = bool(business.agenda_preferences_reviewed)

        values = {
            "company": company,
            "team": team,
            "business_hours": hours,
            "services": services,
            "materials": materials,
            "agenda": agenda,
            "whatsapp": whatsapp,
        }
        completed = sum(values.values())
        next_step = next((key for key, ready in values.items() if not ready), "complete")
        reasons: list[str] = []
        if not company:
            reasons.append("Preencha nome, responsável e endereço da empresa.")
        if not team:
            reasons.append("Cadastre pelo menos um técnico ativo.")
        if not hours:
            reasons.append("Cadastre pelo menos um horário de funcionamento.")
        if not services:
            reasons.append("Mantenha pelo menos um serviço ativo com preço definido.")
        if not materials:
            reasons.append("Ative pelo menos um material ou equipamento com preço definido.")
        if not agenda:
            reasons.append("Revise as preferências de agenda e disponibilidade.")
        if not whatsapp:
            reasons.append("Conecte o WhatsApp Business.")

        return SetupStatus(
            **values,
            completed=completed,
            next_step=next_step,
            onboarding_completed=business.onboarding_completed_at is not None,
            onboarding_completed_at=business.onboarding_completed_at,
            onboarding_version=business.onboarding_version,
            blocking_reasons=reasons,
        )

    async def complete_onboarding(self, business_id: UUID) -> SetupStatus:
        status = await self.setup_status(business_id)
        if status.completed != status.total:
            raise HTTPException(
                409,
                {"message": "Onboarding is incomplete", "blocking_reasons": status.blocking_reasons},
            )
        business = await self._business(business_id, for_update=True)
        if business.onboarding_completed_at is None:
            business.onboarding_completed_at = datetime.now(UTC)
        business.onboarding_version = max(business.onboarding_version, 1)
        await self.session.commit()
        return await self.setup_status(business_id)

    async def _ensure_catalog_presets(self, business_id: UUID) -> None:
        presets = (
            ("extra-tubing-meter", "material", "Metro adicional de tubulação", "Cobrança por metro acima da metragem incluída no serviço.", "metro"),
            ("extra-drain-meter", "material", "Metro adicional de dreno", "Material adicional de drenagem quando necessário.", "metro"),
            ("extra-electrical-cable-meter", "material", "Metro adicional de cabo elétrico", "Cabo elétrico adicional utilizado na instalação.", "metro"),
            ("condenser-bracket", "equipment", "Suporte para condensadora", "Suporte utilizado na instalação da unidade externa.", "unidade"),
            ("wall-bracket-fixings", "material", "Kit de fixação", "Parafusos, buchas e itens de fixação adicionais.", "kit"),
        )
        existing = set((await self.session.scalars(
            select(BusinessCatalogItem.preset_key).where(
                BusinessCatalogItem.business_id == business_id,
                BusinessCatalogItem.preset_key.is_not(None),
            )
        )).all())
        missing = [preset for preset in presets if preset[0] not in existing]
        if not missing:
            return
        self.session.add_all([
            BusinessCatalogItem(
                business_id=business_id,
                preset_key=key,
                kind=kind,
                name=name,
                description=description,
                unit_label=unit_label,
                active=False,
            )
            for key, kind, name, description, unit_label in missing
        ])
        await self.session.commit()

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
        outbound_message = aliased(Message)
        unread_message = aliased(Message)
        last_outbound = select(func.max(outbound_message.created_at)).where(
            outbound_message.business_id == Conversation.business_id,
            outbound_message.conversation_id == Conversation.id,
            outbound_message.direction == "outbound",
        ).correlate(Conversation).scalar_subquery()
        read_boundary = func.greatest(
            func.coalesce(last_outbound, func.to_timestamp(0)),
            func.coalesce(Conversation.last_read_at, func.to_timestamp(0)),
        )
        unread = select(func.count()).select_from(unread_message).where(
            unread_message.business_id == Conversation.business_id,
            unread_message.conversation_id == Conversation.id,
            unread_message.direction == "inbound",
            unread_message.created_at > read_boundary,
        ).correlate(Conversation).scalar_subquery()
        query = select(
            Conversation, Customer.name, Customer.whatsapp_profile_name,
            Customer.phone_e164, Customer.whatsapp_id,
            latest_body.label("last_content"), latest_time.label("last_message_at"),
            latest_direction.label("last_direction"), unread.label("unread_count"),
        ).join(Customer, and_(Customer.business_id == Conversation.business_id, Customer.id == Conversation.customer_id)).where(
            Conversation.business_id == business_id,
            Conversation.deleted_at.is_(None),
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
    unread_value = int(unread_count or 0)
    if item.manual_unread and unread_value == 0:
        unread_value = 1
    return ConversationView(
        id=item.id,
        customer_id=item.customer_id,
        customer_name=_display_name(
            customer_name, whatsapp_profile_name, customer_phone, whatsapp_id
        ),
        customer_phone=customer_phone,
        last_content=last_content,
        last_message_at=last_message_at,
        status=status,
        unread_count=unread_value,
        priority=unread_value > 0 or item.handoff_status == "waiting",
        pinned=item.pinned_at is not None,
        manual_unread=item.manual_unread,
        assignee_name=None,
    )


def _business_view(item: Business) -> BusinessView:
    return BusinessView(
        id=item.id,
        name=item.name,
        responsible_name=item.responsible_name,
        timezone=item.timezone,
        service_origin_address=item.service_origin_address,
        slot_interval_minutes=item.slot_interval_minutes,
        interval_between_services_minutes=item.interval_between_services_minutes,
        preparation_minutes=item.preparation_minutes,
        finishing_minutes=item.finishing_minutes,
        minimum_booking_notice_minutes=item.minimum_booking_notice_minutes,
        materials_catalog_reviewed=item.materials_catalog_reviewed,
        agenda_preferences_reviewed=item.agenda_preferences_reviewed,
        onboarding_completed_at=item.onboarding_completed_at,
        onboarding_version=item.onboarding_version,
    )


def _hours_view(row: Any) -> WorkingHoursView:
    item, employee_name = row
    return WorkingHoursView(
        id=item.id, employee_id=item.employee_id, employee_name=employee_name,
        weekday=item.weekday, start_time=item.start_time, end_time=item.end_time,
    )


def _automation_exclusion_view(item) -> AutomationExclusionView:
    return AutomationExclusionView(
        id=item.id,
        whatsapp_id=item.whatsapp_id,
        mode=item.mode,
        label=item.label,
        reason=item.reason,
        active=item.active,
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
    examples = item.intent_examples or list(generate_service_intent_examples(item.name))
    return ServiceOption(
        id=item.id,
        name=item.name,
        duration_minutes=item.duration_minutes,
        price=item.base_price,
        active=item.active,
        intent_examples=list(examples),
    )


def _catalog_item_view(item: BusinessCatalogItem) -> CatalogItemView:
    return CatalogItemView(
        id=item.id,
        kind=item.kind,
        name=item.name,
        description=item.description,
        price=item.price,
        unit_label=item.unit_label,
        preset_key=item.preset_key,
        active=item.active,
    )

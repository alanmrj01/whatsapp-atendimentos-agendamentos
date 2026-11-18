from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.auth.schemas import AccessMode
from app.models import (
    Business,
    BusinessAccess,
    BusinessUserMembership,
    BusinessWhatsAppConnection,
    BillingCheckout,
    CommercialSubscription,
    MarketingFunnelEvent,
    MarketingLeadContact,
    User,
)
from app.models.billing import billing_provider_environment
from app.operations.defaults import default_services_for_business
from app.platform_admin.schemas import (
    PlatformBusinessCreateRequest,
    PlatformBusinessAccessResponse,
    PlatformBusinessDetailResponse,
    PlatformBusinessListResponse,
    PlatformBusinessResponse,
    PlatformBusinessStatusResponse,
)


class PlatformAdminService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_businesses(self) -> PlatformBusinessListResponse:
        businesses = list((await self.db.scalars(
            select(Business).order_by(Business.name, Business.id)
        )).all())
        if not businesses:
            return PlatformBusinessListResponse(businesses=[])

        ids = [business.id for business in businesses]
        owner_rows = await self.db.execute(
            select(BusinessUserMembership.business_id, User.email)
            .join(User, User.id == BusinessUserMembership.user_id)
            .where(
                BusinessUserMembership.business_id.in_(ids),
                BusinessUserMembership.role == "owner",
            )
            .order_by(User.email)
        )
        owners: dict[UUID, list[str]] = defaultdict(list)
        for business_id, email in owner_rows:
            owners[business_id].append(email)

        connection_rows = await self.db.execute(
            select(
                BusinessWhatsAppConnection.business_id,
                BusinessWhatsAppConnection.status,
                BusinessWhatsAppConnection.display_phone_number,
                BusinessWhatsAppConnection.created_at,
            )
            .where(BusinessWhatsAppConnection.business_id.in_(ids))
            .order_by(BusinessWhatsAppConnection.created_at.desc())
        )
        statuses: dict[UUID, str] = {}
        whatsapp_numbers: dict[UUID, str] = {}
        for business_id, status, display_phone_number, _ in connection_rows:
            statuses.setdefault(business_id, status)
            if display_phone_number:
                whatsapp_numbers.setdefault(business_id, display_phone_number)

        access_rows = await self.db.execute(
            select(
                BusinessAccess.business_id,
                BusinessAccess.access_mode,
                BusinessAccess.admin_full_access,
            ).where(BusinessAccess.business_id.in_(ids))
        )
        access_state = {
            business_id: (access_mode, admin_full_access)
            for business_id, access_mode, admin_full_access in access_rows
        }

        subscription_rows = await self.db.execute(
            select(
                CommercialSubscription.business_id,
                CommercialSubscription.plan_code,
                CommercialSubscription.billing_cycle,
                CommercialSubscription.status,
                CommercialSubscription.access_until,
                CommercialSubscription.cancellation_requested_at,
                CommercialSubscription.refund_status,
                CommercialSubscription.refund_requested_at,
                CommercialSubscription.refunded_at,
                BillingCheckout.provider_payment_id,
                CommercialSubscription.provider_subscription_id,
                BillingCheckout.payer_name,
                BillingCheckout.payer_email,
                BillingCheckout.payer_phone,
                CommercialSubscription.payment_method,
                BillingCheckout.amount_cents,
                BillingCheckout.paid_at,
                CommercialSubscription.created_at,
            )
            .join(
                BillingCheckout,
                BillingCheckout.id == CommercialSubscription.checkout_id,
            )
            .where(
                CommercialSubscription.business_id.in_(ids),
                CommercialSubscription.provider_environment
                == billing_provider_environment(),
            )
            .order_by(
                CommercialSubscription.business_id,
                CommercialSubscription.created_at.desc(),
            )
        )
        subscriptions: dict[
            UUID,
            tuple[
                str,
                str,
                str,
                datetime,
                datetime | None,
                str,
                datetime | None,
                datetime | None,
                str | None,
                str | None,
                str | None,
                str | None,
                str | None,
                str,
                int,
                datetime | None,
            ],
        ] = {}
        for (
            business_id,
            plan_code,
            billing_cycle,
            status,
            access_until,
            cancellation_requested_at,
            refund_status,
            refund_requested_at,
            refunded_at,
            provider_payment_id,
            provider_subscription_id,
            payer_name,
            payer_email,
            payer_phone,
            payment_method,
            amount_cents,
            paid_at,
            _,
        ) in subscription_rows:
            subscriptions.setdefault(
                business_id,
                (
                    plan_code,
                    billing_cycle,
                    status,
                    access_until,
                    cancellation_requested_at,
                    refund_status,
                    refund_requested_at,
                    refunded_at,
                    provider_payment_id,
                    provider_subscription_id,
                    payer_name,
                    payer_email,
                    payer_phone,
                    payment_method,
                    amount_cents,
                    paid_at,
                ),
            )

        lead_rows = await self.db.execute(
            select(
                MarketingLeadContact.business_id,
                MarketingLeadContact.whatsapp_e164,
            ).where(MarketingLeadContact.business_id.in_(ids))
        )
        lead_whatsapps = {
            business_id: whatsapp_e164
            for business_id, whatsapp_e164 in lead_rows
            if whatsapp_e164
        }

        now = datetime.now(UTC)
        return PlatformBusinessListResponse(businesses=[
            PlatformBusinessResponse(
                id=business.id,
                name=business.name,
                timezone=business.timezone,
                active=business.active,
                created_at=business.created_at,
                # Businesses created before explicit access rows retain the
                # existing paid compatibility used by AuthService. This flag is
                # deliberately not overwritten by commercial billing.
                access_mode=access_state.get(business.id, ("paid", False))[0],
                admin_full_access=access_state.get(
                    business.id, ("paid", False)
                )[1],
                commercial_access_active=(
                    business.id in subscriptions
                    and subscriptions[business.id][2] in {"active", "canceled"}
                    and subscriptions[business.id][3] > now
                ),
                commercial_plan_code=(
                    subscriptions[business.id][0]
                    if business.id in subscriptions
                    else None
                ),
                commercial_billing_cycle=(
                    subscriptions[business.id][1]
                    if business.id in subscriptions
                    else None
                ),
                commercial_subscription_status=(
                    subscriptions[business.id][2]
                    if business.id in subscriptions
                    else None
                ),
                commercial_access_until=(
                    subscriptions[business.id][3]
                    if business.id in subscriptions
                    else None
                ),
                commercial_cancellation_requested_at=(
                    subscriptions[business.id][4]
                    if business.id in subscriptions
                    else None
                ),
                commercial_refund_status=(
                    subscriptions[business.id][5]
                    if business.id in subscriptions
                    else None
                ),
                commercial_refund_requested_at=(
                    subscriptions[business.id][6]
                    if business.id in subscriptions
                    else None
                ),
                commercial_refunded_at=(
                    subscriptions[business.id][7]
                    if business.id in subscriptions
                    else None
                ),
                commercial_provider_payment_id=(
                    subscriptions[business.id][8]
                    if business.id in subscriptions
                    else None
                ),
                commercial_provider_subscription_id=(
                    subscriptions[business.id][9]
                    if business.id in subscriptions
                    else None
                ),
                commercial_contact_name=(
                    subscriptions[business.id][10]
                    if business.id in subscriptions
                    and subscriptions[business.id][10]
                    else business.responsible_name or business.name
                ),
                commercial_contact_email=(
                    subscriptions[business.id][11]
                    if business.id in subscriptions
                    and subscriptions[business.id][11]
                    else (owners.get(business.id, [None])[0] if owners.get(business.id) else None)
                ),
                commercial_contact_phone=(
                    subscriptions[business.id][12]
                    if business.id in subscriptions
                    else None
                ),
                commercial_refund_deadline_at=(
                    subscriptions[business.id][6] + timedelta(hours=24)
                    if business.id in subscriptions
                    and subscriptions[business.id][6] is not None
                    and subscriptions[business.id][5] in {"requested", "reconcile_required"}
                    else None
                ),
                commercial_payment_method=(
                    subscriptions[business.id][13]
                    if business.id in subscriptions
                    else None
                ),
                commercial_amount_cents=(
                    subscriptions[business.id][14]
                    if business.id in subscriptions
                    else None
                ),
                commercial_paid_at=(
                    subscriptions[business.id][15]
                    if business.id in subscriptions
                    else None
                ),
                contact_whatsapp=(
                    lead_whatsapps.get(business.id)
                    or (
                        subscriptions[business.id][12]
                        if business.id in subscriptions
                        else None
                    )
                    or whatsapp_numbers.get(business.id)
                ),
                owners=owners.get(business.id, []),
                whatsapp_status=statuses.get(business.id, "disconnected"),
            )
            for business in businesses
        ])


    async def get_business_detail(
        self, business_id: UUID
    ) -> PlatformBusinessDetailResponse:
        business = await self.db.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Business not found")

        listing = await self.list_businesses()
        base = next(
            (item for item in listing.businesses if item.id == business_id),
            None,
        )
        if base is None:
            raise HTTPException(404, "Business not found")

        lead = await self.db.scalar(
            select(MarketingLeadContact)
            .where(MarketingLeadContact.business_id == business_id)
            .limit(1)
        )
        connection = await self.db.scalar(
            select(BusinessWhatsAppConnection)
            .where(BusinessWhatsAppConnection.business_id == business_id)
            .order_by(BusinessWhatsAppConnection.created_at.desc())
            .limit(1)
        )

        commercial_row = (
            await self.db.execute(
                select(
                    CommercialSubscription.created_at,
                    BillingCheckout.created_at,
                    BillingCheckout.status,
                    BillingCheckout.provider_customer_id,
                )
                .join(
                    BillingCheckout,
                    BillingCheckout.id == CommercialSubscription.checkout_id,
                )
                .where(
                    CommercialSubscription.business_id == business_id,
                    CommercialSubscription.provider_environment
                    == billing_provider_environment(),
                )
                .order_by(CommercialSubscription.created_at.desc())
                .limit(1)
            )
        ).first()

        signup_event = await self.db.scalar(
            select(MarketingFunnelEvent)
            .where(
                MarketingFunnelEvent.business_id == business_id,
                MarketingFunnelEvent.event_name == "signup_completed",
            )
            .order_by(MarketingFunnelEvent.occurred_at.asc())
            .limit(1)
        )
        funnel_rows = await self.db.execute(
            select(
                MarketingFunnelEvent.event_name,
                MarketingFunnelEvent.occurred_at,
            )
            .where(MarketingFunnelEvent.business_id == business_id)
            .order_by(MarketingFunnelEvent.occurred_at.asc())
        )
        milestone_names = {
            "signup_completed",
            "preactivation_started",
            "checkout_started",
            "payment_confirmed",
            "whatsapp_connection_completed",
        }
        milestones: dict[str, datetime] = {}
        last_activity_at: datetime | None = None
        for event_name, occurred_at in funnel_rows:
            last_activity_at = occurred_at
            if event_name in milestone_names:
                milestones.setdefault(event_name, occurred_at)

        properties = signup_event.properties if signup_event is not None else {}
        if not isinstance(properties, dict):
            properties = {}

        def acquisition_value(key: str) -> str | None:
            value = properties.get(key)
            if not isinstance(value, str):
                return None
            normalized = value.strip()
            return normalized or None

        return PlatformBusinessDetailResponse(
            **base.model_dump(),
            responsible_name=business.responsible_name,
            lead_name=lead.lead_name if lead is not None else None,
            lead_source=lead.source if lead is not None else None,
            lead_created_at=lead.created_at if lead is not None else None,
            whatsapp_mode=connection.mode if connection is not None else None,
            whatsapp_display_phone=(
                connection.display_phone_number if connection is not None else None
            ),
            whatsapp_connected_at=(
                connection.connected_at if connection is not None else None
            ),
            whatsapp_disconnected_at=(
                connection.disconnected_at if connection is not None else None
            ),
            whatsapp_last_error_code=(
                connection.last_error_code if connection is not None else None
            ),
            whatsapp_meta_review_status=(
                connection.meta_review_status if connection is not None else None
            ),
            commercial_checkout_status=(
                commercial_row[2] if commercial_row is not None else None
            ),
            commercial_provider_customer_id=(
                commercial_row[3] if commercial_row is not None else None
            ),
            commercial_checkout_created_at=(
                commercial_row[1] if commercial_row is not None else None
            ),
            commercial_subscription_created_at=(
                commercial_row[0] if commercial_row is not None else None
            ),
            acquisition_source=acquisition_value("utm_source"),
            acquisition_medium=acquisition_value("utm_medium"),
            acquisition_campaign=acquisition_value("utm_campaign"),
            acquisition_content=acquisition_value("utm_content"),
            acquisition_term=acquisition_value("utm_term"),
            acquisition_campaign_id=acquisition_value("campaign_id"),
            acquisition_adset_id=acquisition_value("adset_id"),
            acquisition_ad_id=acquisition_value("ad_id"),
            acquisition_placement=acquisition_value("placement"),
            acquisition_angle=acquisition_value("angle"),
            acquisition_quiz_profile=(
                acquisition_value("quiz_profile")
                or acquisition_value("result_profile")
            ),
            signup_completed_at=milestones.get("signup_completed"),
            demo_started_at=milestones.get("preactivation_started"),
            checkout_started_at=milestones.get("checkout_started"),
            payment_confirmed_at=milestones.get("payment_confirmed"),
            whatsapp_connected_event_at=milestones.get(
                "whatsapp_connection_completed"
            ),
            last_activity_at=last_activity_at or business.updated_at,
        )

    async def _idempotent_replay(
        self, payload: PlatformBusinessCreateRequest, idempotency_key: UUID
    ) -> PlatformBusinessResponse | None:
        business = await self.db.get(Business, idempotency_key)
        if business is None:
            return None

        listing = await self.list_businesses()
        result = next(
            (item for item in listing.businesses if item.id == business.id),
            None,
        )
        if (
            result is None
            or result.name != payload.name
            or result.timezone != payload.timezone
            or payload.owner_email not in result.owners
        ):
            raise HTTPException(400, "Idempotency key already used for another request")
        return result

    async def create_business(
        self,
        payload: PlatformBusinessCreateRequest,
        idempotency_key: UUID | None = None,
    ) -> PlatformBusinessResponse:
        if idempotency_key is not None:
            replay = await self._idempotent_replay(payload, idempotency_key)
            if replay is not None:
                return replay

        existing = await self.db.scalar(select(User).where(User.email == payload.owner_email))
        if existing is not None:
            raise HTTPException(409, "Owner email already registered")

        password_hash = await to_thread.run_sync(
            hash_password, payload.owner_password.get_secret_value()
        )
        business = Business(
            id=idempotency_key or uuid4(),
            name=payload.name,
            timezone=payload.timezone,
            active=True,
        )
        owner = User(
            id=uuid4(),
            email=payload.owner_email,
            password_hash=password_hash,
            is_active=True,
            platform_role=None,
        )
        access = BusinessAccess(
            business_id=business.id,
            access_mode="paid",
            has_had_operational_access=True,
            admin_full_access=True,
        )
        membership = BusinessUserMembership(
            user_id=owner.id,
            business_id=business.id,
            role="owner",
        )
        self.db.add_all([business, owner])
        try:
            # Persist parent rows inside the same transaction before inserting
            # the membership that references both foreign keys.
            await self.db.flush()
            self.db.add_all(
                [access, membership, *default_services_for_business(business.id)]
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            if idempotency_key is not None:
                # Another copy of this same request may have committed while
                # this one was in flight. Reconcile before reporting a conflict.
                replay = await self._idempotent_replay(payload, idempotency_key)
                if replay is not None:
                    return replay
            raise HTTPException(409, "Business or owner already exists") from None

        return PlatformBusinessResponse(
            id=business.id,
            name=business.name,
            timezone=business.timezone,
            active=True,
            access_mode="paid",
            admin_full_access=True,
            owners=[owner.email],
            whatsapp_status="disconnected",
        )

    async def set_business_active(
        self, business_id: UUID, active: bool
    ) -> PlatformBusinessStatusResponse:
        business = await self.db.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Business not found")
        business.active = active
        await self.db.commit()
        return PlatformBusinessStatusResponse(id=business.id, active=business.active)

    async def set_business_access(
        self, business_id: UUID, access_mode: AccessMode
    ) -> PlatformBusinessAccessResponse:
        business = await self.db.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Business not found")

        # Granting access permanently records that this tenant has operated with
        # real data. Revoking access changes permissions only; it never clears the
        # history flag and therefore never turns the tenant back into a demo.
        history_on_update = (
            True if access_mode == "paid" else BusinessAccess.has_had_operational_access
        )
        statement = (
            insert(BusinessAccess)
            # Missing access rows are legacy paid tenants, so an admin operation on
            # one of them must preserve that real-operational history even on revoke.
            .values(
                business_id=business_id,
                access_mode=access_mode,
                has_had_operational_access=True,
                admin_full_access=(access_mode == "paid"),
            )
            .on_conflict_do_update(
                index_elements=[BusinessAccess.business_id],
                set_={
                    "access_mode": access_mode,
                    "has_had_operational_access": history_on_update,
                    "admin_full_access": (access_mode == "paid"),
                },
            )
            .returning(BusinessAccess.business_id, BusinessAccess.access_mode)
        )
        result = await self.db.execute(statement)
        updated_business_id, updated_access_mode = result.one()
        await self.db.commit()
        return PlatformBusinessAccessResponse(
            business_id=updated_business_id,
            access_mode=updated_access_mode,
        )

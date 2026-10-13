from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Business,
    WhatsAppOnboardingAttempt,
    WhatsAppOnboardingAttemptEvent,
)
from app.whatsapp.administration import (
    META_AUTHORIZATION_INCOMPLETE,
    META_ONBOARDING_EXPIRED,
    WhatsAppConnectionAdministrationError,
    WhatsAppConnectionAdministrationService,
)
from app.whatsapp.connections import WhatsAppConnectionMode, sanitize_error_code


ATTEMPT_TTL = timedelta(minutes=15)
EMPTY_CALLBACK_GRACE = timedelta(seconds=5)

ACTIVE_STATES = frozenset(
    {
        "created",
        "provider_opened",
        "awaiting_provider",
        "code_received",
        "assets_received",
        "completing",
    }
)
TERMINAL_STATES = frozenset(
    {
        "connected",
        "authorization_incomplete",
        "cancelled",
        "failed",
        "expired",
        "superseded",
    }
)


class WhatsAppOnboardingAttemptError(RuntimeError):
    """Sanitized lifecycle error for a Meta onboarding attempt."""


@dataclass(frozen=True, slots=True)
class WhatsAppOnboardingAttemptView:
    id: uuid.UUID
    business_id: uuid.UUID
    connection_id: uuid.UUID
    mode: str
    state: str
    last_stage: str | None
    authorization_code_received: bool
    waba_id_received: bool
    phone_number_id_received: bool
    failure_code: str | None
    empty_callback_at: datetime | None
    last_event_at: datetime
    expires_at: datetime
    terminal_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WhatsAppOnboardingSweepResult:
    scanned: int
    reconciled: int
    failed: int


class WhatsAppOnboardingAttemptService:
    """Coordinates concurrent Embedded Signup attempts server-side.

    The attempt id is the correlation boundary. Browser events from an older
    attempt may still arrive, but they can only mutate that old attempt and
    never a newer one for the same business.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_business(self, business_id: uuid.UUID) -> None:
        business = await self._session.scalar(
            select(Business)
            .where(Business.id == business_id)
            .with_for_update()
        )
        if business is None:
            raise WhatsAppOnboardingAttemptError("Business is not available")

    async def latest(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView | None:
        attempt = await self._session.scalar(
            select(WhatsAppOnboardingAttempt)
            .where(WhatsAppOnboardingAttempt.business_id == business_id)
            .order_by(
                WhatsAppOnboardingAttempt.created_at.desc(),
                WhatsAppOnboardingAttempt.id.desc(),
            )
            .limit(1)
        )
        return _view(attempt) if attempt is not None else None

    async def active(
        self,
        business_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WhatsAppOnboardingAttemptView | None:
        attempt = await self._active_model(
            business_id,
            for_update=for_update,
        )
        return _view(attempt) if attempt is not None else None

    async def due_business_ids(self, *, limit: int = 100) -> list[uuid.UUID]:
        now = datetime.now(timezone.utc)
        empty_callback_due = and_(
            WhatsAppOnboardingAttempt.authorization_code_received.is_(False),
            WhatsAppOnboardingAttempt.empty_callback_at.is_not(None),
            WhatsAppOnboardingAttempt.empty_callback_at
            <= now - EMPTY_CALLBACK_GRACE,
        )
        rows = await self._session.scalars(
            select(WhatsAppOnboardingAttempt.business_id)
            .where(
                WhatsAppOnboardingAttempt.state.in_(ACTIVE_STATES),
                or_(
                    WhatsAppOnboardingAttempt.expires_at <= now,
                    empty_callback_due,
                ),
            )
            .order_by(
                WhatsAppOnboardingAttempt.expires_at,
                WhatsAppOnboardingAttempt.created_at,
            )
            .limit(max(1, min(limit, 500)))
        )
        return list(rows)

    async def begin(
        self,
        business_id: uuid.UUID,
        connection_id: uuid.UUID,
        mode: WhatsAppConnectionMode,
        *,
        request_id: uuid.UUID | None = None,
    ) -> WhatsAppOnboardingAttemptView:
        # Serialize attempt creation per tenant. Different businesses still
        # onboard concurrently without contending on a global lock.
        business = await self._session.scalar(
            select(Business)
            .where(Business.id == business_id)
            .with_for_update()
        )
        if business is None:
            raise WhatsAppOnboardingAttemptError("Business is not available")

        if request_id is not None:
            existing = await self._session.scalar(
                select(WhatsAppOnboardingAttempt).where(
                    WhatsAppOnboardingAttempt.request_id == request_id
                )
            )
            if existing is not None:
                if existing.business_id != business_id:
                    raise WhatsAppOnboardingAttemptError(
                        "Onboarding idempotency key belongs to another business"
                    )
                return _view(existing)

        now = datetime.now(timezone.utc)
        active = await self._active_model(business_id, for_update=True)
        if active is not None:
            if active.expires_at <= now:
                _finish(
                    active,
                    state="expired",
                    failure_code=META_ONBOARDING_EXPIRED,
                    now=now,
                )
                active.last_stage = "server_expired_on_restart"
                self._journal(
                    active,
                    source="server",
                    stage=active.last_stage,
                )
                await self._session.flush()
            elif (
                active.connection_id == connection_id
                and active.mode == mode.value
            ):
                # Network retries and multiple UI reads reuse the same server
                # attempt instead of creating competing provider flows.
                return _view(active)
            else:
                _finish(
                    active,
                    state="superseded",
                    failure_code="META_ONBOARDING_SUPERSEDED",
                    now=now,
                )
                active.last_stage = "server_superseded"
                self._journal(
                    active,
                    source="server",
                    stage=active.last_stage,
                )
                await self._session.flush()

        attempt = WhatsAppOnboardingAttempt(
            business_id=business_id,
            connection_id=connection_id,
            request_id=request_id,
            mode=mode.value,
            state="created",
            last_stage="attempt_started",
            last_event_at=now,
            expires_at=now + ATTEMPT_TTL,
        )
        self._session.add(attempt)
        await self._session.flush()
        self._journal(
            attempt,
            source="server",
            stage="attempt_started",
        )
        await self._session.flush()
        return _view(attempt)

    async def record_event(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        stage: str,
        authorization_code_received: bool | None = None,
        waba_id_received: bool | None = None,
        phone_number_id_received: bool | None = None,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state in TERMINAL_STATES:
            return _view(attempt)

        now = datetime.now(timezone.utc)
        attempt.last_stage = stage
        attempt.last_event_at = now
        attempt.expires_at = now + ATTEMPT_TTL

        if authorization_code_received is True:
            attempt.authorization_code_received = True
        if waba_id_received is True:
            attempt.waba_id_received = True
        if phone_number_id_received is True:
            attempt.phone_number_id_received = True

        if stage == "login_opened":
            attempt.empty_callback_at = None
            attempt.state = "provider_opened"
        elif stage in {
            "page_hidden",
            "page_visible",
            "window_focus",
            "pageshow",
            "popup_closed",
            "sdk_ready",
            "intermediate_step_received",
        }:
            if attempt.state == "created":
                attempt.state = "awaiting_provider"
        elif stage == "wa_session_event_received":
            attempt.state = (
                "assets_received"
                if attempt.waba_id_received
                else "awaiting_provider"
            )
        elif stage == "login_callback_received":
            if authorization_code_received is True:
                attempt.authorization_code_received = True
                attempt.empty_callback_at = None
                attempt.state = "code_received"
            elif (
                authorization_code_received is False
                and not attempt.authorization_code_received
            ):
                # Mobile/PWA can emit a transient empty callback. Persist the
                # observation and let server reconciliation decide after the
                # grace period. The browser is no longer authoritative here.
                attempt.empty_callback_at = attempt.empty_callback_at or now
                attempt.state = "awaiting_provider"
        elif stage == "embedded_signup_ready_to_complete":
            attempt.state = (
                "assets_received"
                if attempt.waba_id_received
                else "code_received"
            )
        elif stage == "complete_request_started":
            attempt.state = "completing"
        elif stage == "complete_request_succeeded":
            _finish(attempt, state="connected", failure_code=None, now=now)
        elif stage == "complete_request_failed":
            _finish(
                attempt,
                state="failed",
                failure_code="META_ONBOARDING_FAILED",
                now=now,
            )
        elif stage == "timeout":
            _finish(
                attempt,
                state="expired",
                failure_code=META_ONBOARDING_EXPIRED,
                now=now,
            )

        self._journal(
            attempt,
            source="client",
            stage=stage,
            authorization_code_received=authorization_code_received,
            waba_id_received=waba_id_received,
            phone_number_id_received=phone_number_id_received,
        )
        await self._session.flush()
        return _view(attempt)

    async def record_assets(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        has_waba_id: bool,
        has_phone_number_id: bool,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state in TERMINAL_STATES:
            return _view(attempt)
        now = datetime.now(timezone.utc)
        attempt.waba_id_received = attempt.waba_id_received or has_waba_id
        attempt.phone_number_id_received = (
            attempt.phone_number_id_received or has_phone_number_id
        )
        attempt.last_stage = "assets_persisted"
        attempt.last_event_at = now
        attempt.expires_at = now + ATTEMPT_TTL
        attempt.state = "assets_received"
        self._journal(
            attempt,
            source="server",
            stage="assets_persisted",
            waba_id_received=has_waba_id,
            phone_number_id_received=has_phone_number_id,
        )
        await self._session.flush()
        return _view(attempt)

    async def mark_completing(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state == "connected":
            return _view(attempt)
        if attempt.state in TERMINAL_STATES:
            raise WhatsAppOnboardingAttemptError(
                "WhatsApp onboarding attempt is no longer active"
            )
        now = datetime.now(timezone.utc)
        attempt.state = "completing"
        attempt.last_stage = "complete_request_started"
        attempt.last_event_at = now
        attempt.expires_at = now + ATTEMPT_TTL
        self._journal(
            attempt,
            source="server",
            stage="complete_request_started",
        )
        await self._session.flush()
        return _view(attempt)

    async def mark_authorization_incomplete(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state == "connected":
            return _view(attempt)
        if attempt.state in TERMINAL_STATES:
            return _view(attempt)
        now = datetime.now(timezone.utc)
        _finish(
            attempt,
            state="authorization_incomplete",
            failure_code=META_AUTHORIZATION_INCOMPLETE,
            now=now,
        )
        attempt.last_stage = "authorization_incomplete"
        self._journal(
            attempt,
            source="server",
            stage=attempt.last_stage,
        )
        await self._session.flush()
        return _view(attempt)

    async def mark_connected(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state == "connected":
            return _view(attempt)
        now = datetime.now(timezone.utc)
        _finish(attempt, state="connected", failure_code=None, now=now)
        attempt.last_stage = "complete_request_succeeded"
        self._journal(
            attempt,
            source="server",
            stage=attempt.last_stage,
        )
        await self._session.flush()
        return _view(attempt)

    async def mark_failed(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
        failure_code: str,
    ) -> WhatsAppOnboardingAttemptView:
        attempt = await self._require_model(
            business_id,
            attempt_id,
            for_update=True,
        )
        if attempt.state == "connected":
            return _view(attempt)
        if attempt.state in TERMINAL_STATES and attempt.state != "failed":
            return _view(attempt)
        normalized = sanitize_error_code(failure_code) or "META_ONBOARDING_FAILED"
        now = datetime.now(timezone.utc)
        _finish(attempt, state="failed", failure_code=normalized, now=now)
        attempt.last_stage = "complete_request_failed"
        self._journal(
            attempt,
            source="server",
            stage=attempt.last_stage,
        )
        await self._session.flush()
        return _view(attempt)

    async def reconcile(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView | None:
        attempt = await self._active_model(business_id, for_update=True)
        if attempt is None:
            return None
        now = datetime.now(timezone.utc)
        if (
            not attempt.authorization_code_received
            and attempt.empty_callback_at is not None
            and attempt.empty_callback_at + EMPTY_CALLBACK_GRACE <= now
        ):
            _finish(
                attempt,
                state="authorization_incomplete",
                failure_code=META_AUTHORIZATION_INCOMPLETE,
                now=now,
            )
            attempt.last_stage = "server_empty_callback_reconciled"
            self._journal(
                attempt,
                source="server",
                stage=attempt.last_stage,
            )
            await self._session.flush()
            return _view(attempt)
        if attempt.expires_at <= now:
            _finish(
                attempt,
                state="expired",
                failure_code=META_ONBOARDING_EXPIRED,
                now=now,
            )
            attempt.last_stage = "server_expired"
            self._journal(
                attempt,
                source="server",
                stage=attempt.last_stage,
            )
            await self._session.flush()
        return _view(attempt)

    async def _active_model(
        self,
        business_id: uuid.UUID,
        *,
        for_update: bool,
    ) -> WhatsAppOnboardingAttempt | None:
        statement = (
            select(WhatsAppOnboardingAttempt)
            .where(
                WhatsAppOnboardingAttempt.business_id == business_id,
                WhatsAppOnboardingAttempt.state.in_(ACTIVE_STATES),
            )
            .order_by(
                WhatsAppOnboardingAttempt.created_at.desc(),
                WhatsAppOnboardingAttempt.id.desc(),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def _require_model(
        self,
        business_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        for_update: bool,
    ) -> WhatsAppOnboardingAttempt:
        statement = select(WhatsAppOnboardingAttempt).where(
            WhatsAppOnboardingAttempt.id == attempt_id,
            WhatsAppOnboardingAttempt.business_id == business_id,
        )
        if for_update:
            statement = statement.with_for_update()
        attempt = await self._session.scalar(statement)
        if attempt is None:
            raise WhatsAppOnboardingAttemptError(
                "WhatsApp onboarding attempt is not available"
            )
        return attempt

    def _journal(
        self,
        attempt: WhatsAppOnboardingAttempt,
        *,
        source: str,
        stage: str,
        authorization_code_received: bool | None = None,
        waba_id_received: bool | None = None,
        phone_number_id_received: bool | None = None,
    ) -> None:
        self._session.add(
            WhatsAppOnboardingAttemptEvent(
                attempt_id=attempt.id,
                business_id=attempt.business_id,
                source=source,
                stage=stage,
                state_after=attempt.state,
                authorization_code_received=authorization_code_received,
                waba_id_received=waba_id_received,
                phone_number_id_received=phone_number_id_received,
                failure_code=attempt.failure_code,
            )
        )


async def reconcile_due_onboarding_attempts(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> WhatsAppOnboardingSweepResult:
    """Reconcile attempts independently from the browser.

    Candidate discovery does not lock attempts. Each business is then locked
    using the same business -> attempt -> connection ordering as interactive
    onboarding, preventing cross-worker deadlocks while allowing unrelated
    tenants to progress concurrently.
    """

    attempts = WhatsAppOnboardingAttemptService(session)
    business_ids = await attempts.due_business_ids(limit=limit)
    await session.commit()

    reconciled = 0
    failed = 0
    for business_id in business_ids:
        try:
            async with session.begin():
                attempts = WhatsAppOnboardingAttemptService(session)
                administration = WhatsAppConnectionAdministrationService(session)
                await attempts.lock_business(business_id)
                attempt = await attempts.reconcile(business_id)
                if attempt is None or attempt.state not in {
                    "authorization_incomplete",
                    "expired",
                }:
                    continue

                connection = await administration.get_connection(
                    business_id,
                    for_update=True,
                )
                if (
                    connection is not None
                    and connection.id == attempt.connection_id
                    and connection.status.value != "connected"
                ):
                    await administration.mark_onboarding_error(
                        business_id,
                        (
                            META_AUTHORIZATION_INCOMPLETE
                            if attempt.state == "authorization_incomplete"
                            else META_ONBOARDING_EXPIRED
                        ),
                    )
                reconciled += 1
        except (
            WhatsAppOnboardingAttemptError,
            WhatsAppConnectionAdministrationError,
        ):
            failed += 1

    return WhatsAppOnboardingSweepResult(
        scanned=len(business_ids),
        reconciled=reconciled,
        failed=failed,
    )


def _finish(
    attempt: WhatsAppOnboardingAttempt,
    *,
    state: str,
    failure_code: str | None,
    now: datetime,
) -> None:
    attempt.state = state
    attempt.failure_code = failure_code
    attempt.terminal_at = now
    attempt.last_event_at = now
    attempt.expires_at = now


def _view(attempt: WhatsAppOnboardingAttempt) -> WhatsAppOnboardingAttemptView:
    return WhatsAppOnboardingAttemptView(
        id=attempt.id,
        business_id=attempt.business_id,
        connection_id=attempt.connection_id,
        mode=attempt.mode,
        state=attempt.state,
        last_stage=attempt.last_stage,
        authorization_code_received=attempt.authorization_code_received,
        waba_id_received=attempt.waba_id_received,
        phone_number_id_received=attempt.phone_number_id_received,
        failure_code=attempt.failure_code,
        empty_callback_at=attempt.empty_callback_at,
        last_event_at=attempt.last_event_at,
        expires_at=attempt.expires_at,
        terminal_at=attempt.terminal_at,
        created_at=attempt.created_at,
    )

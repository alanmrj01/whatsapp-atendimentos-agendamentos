from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, WhatsAppOnboardingAttempt
from app.whatsapp.administration import (
    META_AUTHORIZATION_INCOMPLETE,
    META_ONBOARDING_EXPIRED,
)
from app.whatsapp.connections import WhatsAppConnectionMode, sanitize_error_code


ATTEMPT_TTL = timedelta(minutes=15)

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
    last_event_at: datetime
    expires_at: datetime
    terminal_at: datetime | None
    created_at: datetime


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
        attempt = await self._session.scalar(statement)
        return _view(attempt) if attempt is not None else None

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
                await self._session.flush()

        attempt = WhatsAppOnboardingAttempt(
            business_id=business_id,
            connection_id=connection_id,
            request_id=request_id,
            mode=mode.value,
            state="created",
            last_event_at=now,
            expires_at=now + ATTEMPT_TTL,
        )
        self._session.add(attempt)
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
                attempt.state = "code_received"
            elif (
                authorization_code_received is False
                and not attempt.authorization_code_received
            ):
                _finish(
                    attempt,
                    state="authorization_incomplete",
                    failure_code=META_AUTHORIZATION_INCOMPLETE,
                    now=now,
                )
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
        await self._session.flush()
        return _view(attempt)

    async def reconcile_expired(
        self,
        business_id: uuid.UUID,
    ) -> WhatsAppOnboardingAttemptView | None:
        attempt = await self._active_model(business_id, for_update=True)
        if attempt is None:
            return None
        now = datetime.now(timezone.utc)
        if attempt.expires_at > now:
            return _view(attempt)
        _finish(
            attempt,
            state="expired",
            failure_code=META_ONBOARDING_EXPIRED,
            now=now,
        )
        attempt.last_stage = "server_expired"
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
        last_event_at=attempt.last_event_at,
        expires_at=attempt.expires_at,
        terminal_at=attempt.terminal_at,
        created_at=attempt.created_at,
    )

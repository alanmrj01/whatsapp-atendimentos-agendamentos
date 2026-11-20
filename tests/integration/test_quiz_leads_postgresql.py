"""Physical, disposable PostgreSQL test of quiz contact to registered-account transition."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analytics.meta_ads import MetaAdsReport
from app.api.marketing_tracking import QuizLeadContactRequest, save_quiz_lead_contact
from app.auth.schemas import SignupRequest
from app.auth.service import AuthService
from app.core.config import Settings
from app.models.marketing import MarketingLeadContact, MarketingQuizLead
from app.platform_admin import analytics as analytics_module
from app.platform_admin.analytics import PlatformAnalyticsService
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL, _assert_disposable_database, _async_url,
    migrated_test_database,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Physical test needs a disposable local PostgreSQL database",
    ),
]
assert migrated_test_database


async def test_quiz_contact_capture_then_signup_moves_to_account_list(monkeypatch) -> None:
    _assert_disposable_database(TEST_DATABASE_URL)
    meta_fetch = AsyncMock(return_value=MetaAdsReport(
        status="awaiting_integration", note=None, ads=[],
    ))
    monkeypatch.setattr(analytics_module, "fetch_meta_ads_report", meta_fetch)

    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone, name = "+5512999991234", "Pessoa Teste"
    request = Request({
        "type": "http", "method": "POST",
        "path": "/api/v1/marketing/quiz-leads", "headers": [],
    })
    contact = QuizLeadContactRequest(
        lead_name=name, lead_whatsapp=phone,
        quiz_profile="service_first", contact_permission=True,
    )
    try:
        async with factory() as db:
            # Direct invocation exercises the real persistence function.
            # HTTP Origin enforcement is a separate FastAPI dependency.
            assert await save_quiz_lead_contact(contact, request, db) == {"accepted": True}
            assert await save_quiz_lead_contact(contact, request, db) == {"accepted": True}
            assert await db.scalar(
                select(func.count()).select_from(MarketingQuizLead)
                .where(MarketingQuizLead.whatsapp_e164 == phone)
            ) == 1

            before = await PlatformAnalyticsService(db, Settings(_env_file=None)).summary(7)
            assert [lead.whatsapp_e164 for lead in before.quiz_leads_only] == [phone]
            assert not any(lead.whatsapp_e164 == phone for lead in before.lead_recovery)

            signup = SignupRequest(
                business_name="Empresa Teste",
                email=f"quiz-{uuid4().hex}@example.test",
                password="correct-horse-battery",
                lead_name=name,
                lead_whatsapp=phone,
            )
            user, session, refresh = await AuthService(db).signup(signup)
            assert user.id is not None and session.id is not None and refresh

            assert await db.scalar(
                select(func.count()).select_from(MarketingLeadContact)
                .where(MarketingLeadContact.whatsapp_e164 == phone)
            ) == 1
            assert await db.scalar(
                select(func.count()).select_from(MarketingQuizLead)
                .where(MarketingQuizLead.whatsapp_e164 == phone)
            ) == 1

            after = await PlatformAnalyticsService(db, Settings(_env_file=None)).summary(7)
            assert not any(lead.whatsapp_e164 == phone for lead in after.quiz_leads_only)
            registered = [lead for lead in after.lead_recovery if lead.whatsapp_e164 == phone]
            assert len(registered) == 1 and registered[0].lead_name == name
            assert meta_fetch.await_count == 2
    finally:
        await engine.dispose()

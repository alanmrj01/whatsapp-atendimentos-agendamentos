from __future__ import annotations

import logging
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.diagnostics import repository as diagnostic_repository
from app.diagnostics.models import AutomationStatus, DiagnosticCode as Code, DiagnosticStatus as Status
from app.diagnostics.repository import DiagnosticsRepository
from app.diagnostics.service import DiagnosticsService, observe
from app.models import (
    Business, BusinessAutomationExclusion, BusinessWhatsAppConnection,
    Conversation, Customer, Message, ProcessedWebhook,
)
from tests.integration.test_booking_postgresql import (
    TEST_DATABASE_URL, _async_url, migrated_test_database,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL não configurada; PostgreSQL físico não executado",
)]
assert migrated_test_database


@pytest_asyncio.fixture
async def diagnostic_db():
    engine = create_async_engine(_async_url(TEST_DATABASE_URL), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        for model in (Message, ProcessedWebhook, Conversation, Customer,
                      BusinessAutomationExclusion, BusinessWhatsAppConnection, Business):
            await session.execute(delete(model))
    try:
        yield engine, factory
    finally:
        await engine.dispose()


async def seed(factory):
    business_a, business_b = uuid4(), uuid4()
    customer_a, customer_b = uuid4(), uuid4()
    conversation_a, conversation_b = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add_all([
            Business(id=business_a, name="private-business-a", meta_phone_number_id="test-phone-number-id"),
            Business(id=business_b, name="private-business-b"),
        ])
        await session.flush()
        session.add_all([
            Customer(id=customer_a, business_id=business_a, whatsapp_id="5511999999901", name="private-customer"),
            Customer(id=customer_b, business_id=business_b, whatsapp_id="5511999999902"),
            BusinessWhatsAppConnection(business_id=business_b, mode="coexistence", status="disconnected",
                meta_phone_number_id="private-phone-b", display_phone_number="5511999999902",
                credential_secret_ref="projects/private/secrets/private/versions/latest", last_error_code="PRIVATE_ERROR"),
        ])
        await session.flush()
        session.add_all([
            Conversation(id=conversation_a, business_id=business_a, customer_id=customer_a, state="start"),
            Conversation(id=conversation_b, business_id=business_b, customer_id=customer_b, state="start",
                automation_suppressed_until=now + timedelta(hours=2), suppression_reason="manual_business_message"),
        ])
        await session.flush()
        for business, conversation, direction, status, age in (
            (business_a, conversation_a, "inbound", "received", 1),
            (business_a, conversation_a, "outbound", "sent", 1),
            (business_a, conversation_a, "outbound", "pending", 1),
            (business_a, conversation_a, "outbound", "pending", 48),
            (business_a, conversation_a, "outbound", "failed", 48),
            (business_b, conversation_b, "outbound", "failed", 1),
            (business_b, conversation_b, "outbound", "failed", 1),
        ):
            session.add(Message(business_id=business, conversation_id=conversation,
                direction=direction, status=status, message_type="text", body="private-message-content",
                outbound_payload={"text": "private-payload"} if direction == "outbound" else None,
                created_at=now - timedelta(hours=age), updated_at=now - timedelta(hours=age)))
        session.add(ProcessedWebhook(event_key="private-event", event_type="message", status="processed",
            received_at=now - timedelta(hours=1), processed_at=now))
    return business_a, business_b


async def test_physical_reports_are_scoped_sanitized_and_read_only(diagnostic_db, caplog):
    engine, factory = diagnostic_db
    business_a, business_b = await seed(factory)
    service = DiagnosticsService(DiagnosticsRepository(lambda: engine), Settings(_env_file=None))
    caplog.set_level(logging.INFO)
    report = await service.diagnostics(True)
    a = await service.business_diagnostics(business_a)
    b = await service.business_diagnostics(business_b)
    assert report.status is Status.DEGRADED
    assert report.components.migration.code is Code.MIGRATION_OK
    assert report.components.whatsapp.details.connections.disconnected == 1
    assert report.components.whatsapp.details.legacy_pilot_present is True
    assert report.components.whatsapp.details.credential_references_configured == 1
    assert a.status is Status.OK
    assert a.automation_status is AutomationStatus.ENABLED
    assert a.legacy_pilot and a.connection_status is None
    assert a.pending_outbound_count == 2 and a.failed_outbound_count == 0
    assert a.activity.pending_scope == "all_ages"
    assert a.last_inbound_at is not None and a.last_successful_outbound_at is not None
    assert b.automation_status is AutomationStatus.HUMAN_CONTROLLED
    assert b.pending_outbound_count == 0 and b.failed_outbound_count == 2
    assert b.last_inbound_at is None and b.last_successful_outbound_at is None
    assert b.last_sanitized_error_code is Code.META_CONNECTION_ERROR
    assert report.components.recent_activity.details.last_webhook_processed_at is not None
    assert a.activity.last_webhook_processed_at is None  # No tenant key on webhook schema.
    assert await service.business_diagnostics(uuid4()) is None
    serialized = report.model_dump_json() + a.model_dump_json() + b.model_dump_json() + caplog.text
    for sensitive in ("private-", "PRIVATE_ERROR", "5511999999901", "5511999999902",
                      "test-access-token", "test-phone-number-id", "test-waba-id", "postgresql"):
        assert sensitive not in serialized
    async with engine.connect() as connection:
        # Diagnostics did not mutate messages, schema or human-control state.
        assert await connection.scalar(text("SELECT count(*) FROM messages")) == 7
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260902_0005"
        assert await connection.scalar(text("SELECT count(*) FROM conversations WHERE automation_suppressed_until IS NOT NULL")) == 1

    # Expiry is observed without changing the business's human-control policy.
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE conversations SET automation_suppressed_until = :expired WHERE business_id = :business_id"),
            {"expired": datetime.now(UTC) - timedelta(minutes=1), "business_id": business_b})
    assert (await service.business_diagnostics(business_b)).automation_status is AutomationStatus.ENABLED
    async with factory() as session, session.begin():
        session.add(BusinessAutomationExclusion(business_id=business_b, whatsapp_id="5511999999902", mode="human_only"))
    assert (await service.business_diagnostics(business_b)).automation_status is AutomationStatus.MIXED
    assert (await service.business_diagnostics(business_a)).automation_status is AutomationStatus.ENABLED


async def test_physical_queries_enforce_read_only_and_short_server_timeout(diagnostic_db):
    engine, _ = diagnostic_db
    repo = DiagnosticsRepository(lambda: engine)
    flags = await repo._read("SELECT current_setting('transaction_read_only') AS ro, current_setting('statement_timeout') AS timeout")
    assert flags == [{"ro": "on", "timeout": "1500ms"}]
    with pytest.raises(DBAPIError):
        await repo._read("DELETE FROM businesses WHERE false")
    _, code, _ = await observe(lambda: repo._read("SELECT pg_sleep(3)"))
    assert code is Code.DB_TIMEOUT
    await repo.ping()  # Cancellation/rollback does not poison subsequent requests.


async def test_physical_schema_mismatch_is_not_ready_without_running_migration(diagnostic_db):
    engine, _ = diagnostic_db
    service = DiagnosticsService(DiagnosticsRepository(lambda: engine), Settings(_env_file=None))
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num = '20260902_0004'"))
    try:
        report = await service.readiness(True)
        assert report.ready is False
        assert report.migration.code is Code.MIGRATION_BEHIND
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260902_0004"
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = '20260902_0005'"))


async def test_physical_activity_limits_are_explicit(diagnostic_db, monkeypatch):
    engine, factory = diagnostic_db
    await seed(factory)
    monkeypatch.setattr(diagnostic_repository, "OBSERVATION_LIMIT", 2)
    activity = await DiagnosticsRepository(lambda: engine).activity(datetime.now(UTC) - timedelta(hours=24))
    assert activity.truncated is True
    assert activity.pending_outbound_count <= 3
    assert activity.failed_outbound_count <= 3


async def test_physical_connection_summary_ignores_superseded_disconnection(diagnostic_db):
    engine, factory = diagnostic_db
    business_id = uuid4()
    async with factory() as session, session.begin():
        session.add(Business(id=business_id, name="replacement-test"))
        await session.flush()
        session.add_all([
            BusinessWhatsAppConnection(business_id=business_id, mode="api_only", status="disconnected"),
            BusinessWhatsAppConnection(business_id=business_id, mode="api_only", status="connected",
                meta_phone_number_id="test-phone-number-id", graph_version="v23.0"),
        ])
    report = await DiagnosticsService(DiagnosticsRepository(lambda: engine), Settings(_env_file=None)).diagnostics(True)
    assert report.components.whatsapp.details.connections.disconnected == 1
    assert report.components.whatsapp.details.current_disconnected_count == 0
    assert report.status is Status.OK


async def test_physical_work_is_capped_before_tenant_and_pending_filters(diagnostic_db, monkeypatch):
    engine, factory = diagnostic_db
    business_a, _ = await seed(factory)
    async with factory() as session, session.begin():
        conversation_id = await session.scalar(text("SELECT id FROM conversations WHERE business_id = :business_id"),
            {"business_id": business_a})
        session.add_all([
            Message(business_id=business_a, conversation_id=conversation_id, direction="inbound",
                status="pending", message_type="text", created_at=datetime.now(UTC) - timedelta(days=3))
            for _ in range(2_000)
        ])
    async with engine.begin() as connection:
        await connection.execute(text("ANALYZE messages"))
    monkeypatch.setattr(diagnostic_repository, "OBSERVATION_LIMIT", 3)
    repo = DiagnosticsRepository(lambda: engine)
    captured = []
    original = repo._read

    async def capture(sql, parameters=None):
        captured.append((sql, parameters))
        return await original(sql, parameters)

    monkeypatch.setattr(repo, "_read", capture)
    activity = await repo.activity(datetime.now(UTC) - timedelta(hours=24), business_a)
    assert activity.selection == "bounded_business"
    assert activity.sample_limit == 3 and activity.truncated
    assert activity.pending_outbound_count <= 4
    assert len(captured) == 1
    plans = await original("EXPLAIN (ANALYZE, FORMAT JSON) " + captured[0][0], captured[0][1])
    plan = plans[0]["QUERY PLAN"]
    if isinstance(plan, str):
        plan = json.loads(plan)

    def scan_nodes(node):
        if node.get("Relation Name") == "messages":
            yield node
        for child in node.get("Plans", []):
            yield from scan_nodes(child)

    scans = list(scan_nodes(plan[0]["Plan"]))
    assert len(scans) == 1
    assert scans[0]["Actual Rows"] <= 4
    captured.clear()
    global_activity = await repo.activity(datetime.now(UTC) - timedelta(hours=24))
    assert global_activity.selection == "recent_global"
    # A large set of non-outbound pending rows must not cause an unbounded scan.
    assert global_activity.truncated and global_activity.pending_outbound_count <= 4
    assert len(captured) == 2  # One messages aggregate, one webhook aggregate.


async def test_global_query_count_does_not_grow_with_number_of_businesses(diagnostic_db, monkeypatch):
    engine, factory = diagnostic_db
    await seed(factory)
    repo = DiagnosticsRepository(lambda: engine)
    service = DiagnosticsService(repo, Settings(_env_file=None))
    statements = []
    original = repo._read

    async def capture(sql, parameters=None):
        statements.append(sql)
        return await original(sql, parameters)

    monkeypatch.setattr(repo, "_read", capture)
    await service.diagnostics(True)
    before = len(statements)
    async with factory() as session, session.begin():
        for _ in range(30):
            business = Business(name="diagnostic-count-test")
            session.add(business)
            await session.flush()
            session.add(BusinessWhatsAppConnection(business_id=business.id, mode="api_only", status="pending"))
    statements.clear()
    report = await service.diagnostics(True)
    assert report.components.whatsapp.details.connections.pending == 30
    assert len(statements) == before == 5


async def test_oversized_connection_history_is_unknown_not_wrong_connection(diagnostic_db, monkeypatch):
    from app.diagnostics import service as service_module
    engine, factory = diagnostic_db
    business_a, _ = await seed(factory)
    async with factory() as session, session.begin():
        session.add_all([
            BusinessWhatsAppConnection(business_id=business_a, mode="api_only", status="disconnected")
            for _ in range(4)
        ])
    monkeypatch.setattr(diagnostic_repository, "OBSERVATION_LIMIT", 2)
    monkeypatch.setattr(service_module, "OBSERVATION_LIMIT", 2)
    report = await DiagnosticsService(DiagnosticsRepository(lambda: engine), Settings(_env_file=None)).business_diagnostics(business_a)
    assert report.status is Status.UNKNOWN
    assert report.code is Code.OBSERVATION_TRUNCATED
    assert report.connection_status is None and report.connection_mode is None

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.diagnostics.models import ActivityDetails, OBSERVATION_LIMIT


# No ORM entities/payloads are loaded. Every high-volume observation is capped,
# reports truncation, and uses existing business/date/PK indexes. The DB timeout
# also bounds work when an index cannot satisfy the chosen query plan.
class DiagnosticsRepository:
    def __init__(self, engine_provider: Callable[[], AsyncEngine]) -> None:
        self._engine_provider = engine_provider

    async def _read(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        async with self._engine_provider().connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                result = await connection.execute(text(sql), parameters or {})
                return [dict(row) for row in result.mappings()]

    async def ping(self) -> None:
        await self._read("SELECT 1")

    async def revisions(self) -> list[str]:
        rows = await self._read("SELECT version_num FROM public.alembic_version LIMIT 2")
        return [row["version_num"] for row in rows]

    async def whatsapp_summary(self, phone_id: str | None) -> dict[str, Any]:
        return (await self._read("""
            WITH sampled AS MATERIALIZED (
                SELECT id, business_id, created_at, status,
                    credential_secret_ref IS NOT NULL AS has_reference,
                    meta_phone_number_id IS NOT NULL AND
                        meta_phone_number_id ~ '^[A-Za-z0-9_-]{1,255}$' AS has_phone,
                    meta_phone_number_id = :phone_id AS matches_legacy,
                    graph_version IS NOT NULL AS has_graph
                FROM business_whatsapp_connections ORDER BY id LIMIT :limit
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY business_id
                    ORDER BY (status <> 'disconnected') DESC, created_at DESC, id DESC
                ) AS connection_rank FROM sampled
            )
            SELECT count(*) AS observed,
                count(*) FILTER (WHERE status = 'connected') AS connected,
                count(*) FILTER (WHERE status = 'pending') AS pending,
                count(*) FILTER (WHERE status = 'disconnected') AS disconnected,
                count(*) FILTER (WHERE status = 'disconnected' AND connection_rank = 1) AS current_disconnected,
                count(*) FILTER (WHERE status = 'error') AS error,
                count(*) FILTER (WHERE has_reference) AS reference_count,
                count(*) FILTER (WHERE status = 'connected' AND NOT has_phone) AS missing_phone,
                count(*) FILTER (WHERE status = 'connected') AS credential_required,
                count(*) FILTER (WHERE status = 'connected' AND NOT has_reference
                    AND matches_legacy) AS legacy_credential_eligible,
                count(*) FILTER (WHERE status = 'connected' AND NOT has_graph) AS missing_graph,
                EXISTS (
                    SELECT 1 FROM businesses b WHERE b.meta_phone_number_id = :phone_id
                    AND NOT EXISTS (SELECT 1 FROM business_whatsapp_connections c
                        WHERE c.business_id = b.id)
                ) AS legacy_pilot
            FROM ranked
        """, {"phone_id": phone_id, "limit": OBSERVATION_LIMIT + 1}))[0]

    async def activity(
        self, since: datetime, business_id: UUID | None = None
    ) -> ActivityDetails:
        # Global date/status indexes exist, but (business_id, created_at) does
        # not. Fence a bounded tenant sample BEFORE filtering/sorting; never
        # scan another tenant or sort an entire business history to find a tail.
        # These are fixed SQL fragments, not interpolated user input.
        if business_id is None:
            samples = """
            pending_sampled AS MATERIALIZED (
                SELECT direction FROM messages WHERE status = 'pending' LIMIT :limit
            ), sampled AS MATERIALIZED (
                SELECT direction, status, created_at, updated_at FROM messages
                WHERE created_at >= :since
                ORDER BY created_at DESC LIMIT :limit
            )
            """
            observed = "SELECT count(*) FROM sampled"
        else:
            samples = """
            business_sample AS MATERIALIZED (
                SELECT direction, status, created_at, updated_at FROM messages
                WHERE business_id = :business_id LIMIT :limit
            ), sampled AS (
                SELECT * FROM business_sample WHERE created_at >= :since
            ), pending_sampled AS (
                SELECT direction FROM business_sample WHERE status = 'pending'
            )
            """
            observed = "SELECT count(*) FROM business_sample"
        row = (await self._read(f"""
            WITH {samples}
            SELECT ({observed}) AS observed,
                (SELECT count(*) FROM pending_sampled) AS pending_observed,
                max(created_at) FILTER (WHERE direction = 'inbound') AS last_inbound_at,
                max(created_at) FILTER (WHERE direction = 'outbound') AS last_outbound_at,
                max(updated_at) FILTER (WHERE direction = 'outbound'
                    AND status IN ('sent', 'delivered', 'read')) AS last_successful_outbound_at,
                (SELECT count(*) FROM pending_sampled WHERE direction = 'outbound') AS pending_outbound_count,
                count(*) FILTER (WHERE direction = 'outbound' AND status = 'failed') AS failed_outbound_count
            FROM sampled
        """, {"since": since, "business_id": business_id, "limit": OBSERVATION_LIMIT + 1}))[0]
        observed_count = row.pop("observed")
        pending_count = row.pop("pending_observed")
        truncated = max(observed_count, pending_count) > OBSERVATION_LIMIT
        webhook_at = None
        if business_id is None:
            # ProcessedWebhook has no business_id: never pretend it is scoped.
            webhook = (await self._read("""
                WITH sampled AS MATERIALIZED (
                    SELECT processed_at FROM processed_webhooks
                    WHERE received_at >= :since ORDER BY received_at DESC LIMIT :limit
                )
                SELECT count(*) AS observed, max(processed_at) AS processed_at FROM sampled
            """, {"since": since, "limit": OBSERVATION_LIMIT + 1}))[0]
            truncated = truncated or webhook["observed"] > OBSERVATION_LIMIT
            webhook_at = webhook["processed_at"]
        return ActivityDetails(
            sampled_since=since, sample_limit=OBSERVATION_LIMIT, truncated=truncated,
            selection="bounded_business" if business_id is not None else "recent_global",
            last_webhook_processed_at=webhook_at, **row,
        )

    async def business_snapshot(
        self, business_id: UUID, phone_id: str | None, now: datetime
    ) -> dict[str, Any] | None:
        rows = await self._read("""
            WITH connections_sample AS MATERIALIZED (
                SELECT id, mode, status, credential_secret_ref, meta_phone_number_id,
                    graph_version, last_error_code, created_at FROM business_whatsapp_connections
                WHERE business_id = :business_id LIMIT :limit
            )
            SELECT b.active, c.mode, c.status,
                (SELECT count(*) FROM connections_sample) AS connections_observed,
                c.id IS NOT NULL AS has_connection,
                c.credential_secret_ref IS NOT NULL AS has_reference,
                c.meta_phone_number_id IS NOT NULL AND
                    c.meta_phone_number_id ~ '^[A-Za-z0-9_-]{1,255}$' AS has_phone,
                c.meta_phone_number_id = :phone_id AS matches_legacy,
                c.graph_version IS NOT NULL AS has_graph,
                c.last_error_code IS NOT NULL AS has_error,
                c.id IS NULL AND b.meta_phone_number_id = :phone_id AS legacy_pilot
            FROM businesses b LEFT JOIN LATERAL (
                SELECT id, mode, status, credential_secret_ref, meta_phone_number_id,
                    graph_version, last_error_code FROM connections_sample
                ORDER BY (status <> 'disconnected') DESC, created_at DESC, id DESC LIMIT 1
            ) c ON true WHERE b.id = :business_id
        """, {"business_id": business_id, "phone_id": phone_id, "limit": OBSERVATION_LIMIT + 1})
        if not rows:
            return None
        row = rows[0]
        automation = (await self._read("""
            WITH sampled AS MATERIALIZED (
                SELECT automation_enabled, handoff_status, automation_suppressed_until
                FROM conversations WHERE business_id = :business_id
                LIMIT :limit
            ), exclusions AS MATERIALIZED (
                SELECT active FROM business_automation_exclusions
                WHERE business_id = :business_id LIMIT :limit
            )
            SELECT count(*) AS observed,
                count(*) FILTER (WHERE NOT automation_enabled OR handoff_status <> 'none'
                    OR automation_suppressed_until > :now) AS blocked,
                (SELECT count(*) FROM exclusions) AS exclusions_observed,
                EXISTS (SELECT 1 FROM exclusions WHERE active) AS has_exclusions
            FROM sampled
        """, {"business_id": business_id, "now": now, "limit": OBSERVATION_LIMIT + 1}))[0]
        return {**row, **automation}

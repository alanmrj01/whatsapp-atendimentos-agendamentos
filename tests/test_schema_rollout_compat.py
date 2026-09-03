from app.diagnostics.models import (
    DiagnosticCode as Code,
    DiagnosticStatus as Status,
    EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import migration_result


def test_rollout_accepts_current_and_target_schema_revisions() -> None:
    for revision in ("20260902_0005", EXPECTED_SCHEMA_REVISION):
        result = migration_result([revision], 1)
        assert result.status is Status.OK
        assert result.code is Code.MIGRATION_OK
        assert result.details.current_revision == revision


def test_rollout_keeps_other_known_or_future_revisions_fail_closed() -> None:
    behind = migration_result(["20260902_0004"], 1)
    ahead = migration_result(["20260904_0007"], 1)

    assert behind.status is Status.ERROR
    assert behind.code is Code.MIGRATION_BEHIND
    assert ahead.status is Status.ERROR
    assert ahead.code is Code.MIGRATION_AHEAD

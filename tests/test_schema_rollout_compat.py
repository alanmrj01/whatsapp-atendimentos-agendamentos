from app.diagnostics.models import (
    DiagnosticCode as Code,
    DiagnosticStatus as Status,
    EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import migration_result


def test_readiness_accepts_rollout_compatible_schema_revisions() -> None:
    current = migration_result([EXPECTED_SCHEMA_REVISION], 1)
    previous = migration_result(["20260915_0009"], 1)

    assert EXPECTED_SCHEMA_REVISION == "20260915_0010"

    assert current.status is Status.OK
    assert current.code is Code.MIGRATION_OK
    assert current.details.current_revision == "20260915_0010"

    assert previous.status is Status.OK
    assert previous.code is Code.MIGRATION_OK
    assert previous.details.current_revision == "20260915_0009"


def test_readiness_keeps_older_or_future_revisions_fail_closed() -> None:
    behind = migration_result(["20260902_0004"], 1)
    ahead = migration_result(["20260916_0011"], 1)

    assert behind.status is Status.ERROR
    assert behind.code is Code.MIGRATION_BEHIND

    assert ahead.status is Status.ERROR
    assert ahead.code is Code.MIGRATION_AHEAD

from app.diagnostics.models import (
    DiagnosticCode as Code,
    DiagnosticStatus as Status,
    EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import migration_result


def test_readiness_accepts_additive_0043_to_0044_rollout_window() -> None:
    current = migration_result([EXPECTED_SCHEMA_REVISION], 1)
    previous_compatible = migration_result(["20261107_0043"], 1)
    older = migration_result(["20261102_0042"], 1)

    assert EXPECTED_SCHEMA_REVISION == "20261111_0044"

    assert current.status is Status.OK
    assert current.code is Code.MIGRATION_OK
    assert current.details.current_revision == "20261111_0044"

    assert previous_compatible.status is Status.OK
    assert previous_compatible.code is Code.MIGRATION_OK
    assert previous_compatible.details.current_revision == "20261107_0043"

    assert older.status is Status.ERROR
    assert older.code is Code.MIGRATION_BEHIND


def test_readiness_keeps_older_or_future_revisions_fail_closed() -> None:
    behind = migration_result(["20261003_0028"], 1)
    ahead = migration_result(["20261112_0045"], 1)

    assert behind.status is Status.ERROR
    assert behind.code is Code.MIGRATION_BEHIND

    assert ahead.status is Status.ERROR
    assert ahead.code is Code.MIGRATION_AHEAD

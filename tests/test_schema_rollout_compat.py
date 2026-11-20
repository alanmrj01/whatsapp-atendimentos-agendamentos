from app.diagnostics.models import (
    COMPATIBLE_SCHEMA_REVISIONS,
    DiagnosticCode as Code,
    DiagnosticStatus as Status,
    EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import migration_result


def test_bridge_accepts_additive_0043_to_0047_without_upgrading_0046_head() -> None:
    compatible = (
        "20261107_0043",
        "20261111_0044",
        "20261118_0045",
        "20261118_0046",
        "20261120_0047",
    )

    assert EXPECTED_SCHEMA_REVISION == "20261118_0046"
    assert COMPATIBLE_SCHEMA_REVISIONS == compatible

    for revision in compatible:
        result = migration_result([revision], 1)

        assert result.status is Status.OK
        assert result.code is Code.MIGRATION_OK
        assert result.details.current_revision == revision

    older = migration_result(["20261102_0042"], 1)

    assert older.status is Status.ERROR
    assert older.code is Code.MIGRATION_BEHIND


def test_readiness_keeps_older_or_future_revisions_fail_closed() -> None:
    behind = migration_result(["20261003_0028"], 1)
    ahead = migration_result(["20261121_0048"], 1)

    assert behind.status is Status.ERROR
    assert behind.code is Code.MIGRATION_BEHIND

    assert ahead.status is Status.ERROR
    assert ahead.code is Code.MIGRATION_AHEAD

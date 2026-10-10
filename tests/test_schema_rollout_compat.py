from app.diagnostics.models import (
    DiagnosticCode as Code,
    DiagnosticStatus as Status,
    EXPECTED_SCHEMA_REVISION,
)
from app.diagnostics.service import migration_result


def test_readiness_requires_whatsapp_mode_schema_revision() -> None:
    current = migration_result([EXPECTED_SCHEMA_REVISION], 1)
    previous = migration_result(["20261004_0029"], 1)

    assert EXPECTED_SCHEMA_REVISION == "20261010_0031"

    assert current.status is Status.OK
    assert current.code is Code.MIGRATION_OK
    assert current.details.current_revision == "20261010_0031"

    assert previous.status is Status.ERROR
    assert previous.code is Code.MIGRATION_BEHIND
    assert previous.details.current_revision == "20261004_0029"


def test_readiness_keeps_older_or_future_revisions_fail_closed() -> None:
    behind = migration_result(["20261003_0028"], 1)
    ahead = migration_result(["20261009_0031"], 1)

    assert behind.status is Status.ERROR
    assert behind.code is Code.MIGRATION_BEHIND

    assert ahead.status is Status.ERROR
    assert ahead.code is Code.MIGRATION_AHEAD

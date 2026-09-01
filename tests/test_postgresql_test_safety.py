import pytest

from tests.integration.test_booking_postgresql import _assert_disposable_database


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:password@db.example.com:5432/whatsapp_test",
        "postgresql://user:password@localhost:5432/production",
    ],
)
def test_physical_database_guard_rejects_non_disposable_targets(url: str) -> None:
    with pytest.raises(RuntimeError):
        _assert_disposable_database(url)


@pytest.mark.parametrize(
    "host",
    [
        "db.project.supabase.co",
        "aws-0-region.pooler.supabase.com",
    ],
)
def test_physical_database_guard_explicitly_rejects_supabase(host: str) -> None:
    with pytest.raises(RuntimeError, match="não pode apontar para Supabase"):
        _assert_disposable_database(
            f"postgresql://user:password@{host}:5432/whatsapp_test"
        )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_physical_database_guard_accepts_local_test_database(host: str) -> None:
    formatted_host = f"[{host}]" if host == "::1" else host
    _assert_disposable_database(
        f"postgresql://user:password@{formatted_host}:5432/whatsapp_test"
    )

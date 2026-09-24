from datetime import UTC, datetime

from app.conversations.dialogue import daypart_greeting


def test_daypart_greeting_uses_business_timezone() -> None:
    assert daypart_greeting(
        "America/Sao_Paulo",
        now=datetime(2026, 9, 24, 11, 0, tzinfo=UTC),
    ) == "Bom dia"
    assert daypart_greeting(
        "America/Sao_Paulo",
        now=datetime(2026, 9, 24, 16, 0, tzinfo=UTC),
    ) == "Boa tarde"
    assert daypart_greeting(
        "America/Sao_Paulo",
        now=datetime(2026, 9, 24, 23, 0, tzinfo=UTC),
    ) == "Boa noite"

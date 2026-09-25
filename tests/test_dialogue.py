from datetime import UTC, datetime

from app.conversations.dialogue import conversational_greeting, daypart_greeting


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



def test_conversational_greeting_mirrors_explicit_customer_greeting() -> None:
    assert conversational_greeting(
        "Oi, boa tarde, tudo bem?",
        "America/Sao_Paulo",
        customer_name="Alan",
    ) == "Oi, boa tarde, Alan! Tudo bem, e com você? Como posso te ajudar?"


def test_conversational_greeting_can_acknowledge_without_reasking_need() -> None:
    result = conversational_greeting(
        "Bom dia! Preciso de uma limpeza",
        "America/Sao_Paulo",
        include_help=False,
    )

    assert result == "Bom dia!"
    assert "Como posso" not in result

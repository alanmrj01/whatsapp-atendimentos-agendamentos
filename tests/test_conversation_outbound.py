from __future__ import annotations

from decimal import Decimal

from app.conversations.outbound import (
    WHATSAPP_REPLY_BUTTON_TITLE_MAX,
    _button_payload,
    equipment_delivery_message,
    media_received_message,
    unsupported_media_message,
    tubing_guidance_message,
    tubing_variation_message,
)
from app.conversations.ports import BookingOption


def test_purchase_and_install_delivery_button_is_meta_compatible() -> None:
    message = equipment_delivery_message(include_with_installation=True)

    buttons = (message.outbound_payload or {}).get("buttons", [])
    titles = [button["title"] for button in buttons]

    assert "Com a instalação" in titles
    assert all(
        len(title) <= WHATSAPP_REPLY_BUTTON_TITLE_MAX
        for title in titles
    )


def test_button_payload_defensively_limits_future_long_titles() -> None:
    payload = _button_payload(
        (
            BookingOption(
                "delivery.future",
                "Uma opção de botão propositalmente longa demais",
            ),
        )
    )

    title = payload["buttons"][0]["title"]
    assert len(title) <= WHATSAPP_REPLY_BUTTON_TITLE_MAX
    assert title.endswith("…")


def test_tubing_price_copy_respects_configured_catalog_unit() -> None:
    fixed = tubing_variation_message(
        Decimal("3"),
        Decimal("50.00"),
        "valor fixo",
    )
    kit = tubing_guidance_message(
        Decimal("3"),
        Decimal("30.00"),
        "kit",
    )
    unit = tubing_guidance_message(
        Decimal("3"),
        Decimal("15.00"),
        "unidade",
    )

    assert "valor fixo de R$ 50,00" in (fixed.body or "")
    assert "cada kit adicional custa r$ 30,00" in (kit.body or "").casefold()
    assert "cada unidade adicional custa r$ 15,00" in (unit.body or "").casefold()
    for body in (fixed.body or "", kit.body or "", unit.body or ""):
        assert "valor cadastrado" not in body.casefold()


def test_tubing_price_copy_is_customer_language_not_internal_configuration() -> None:
    variation = tubing_variation_message(
        Decimal("3"),
        Decimal("20.00"),
    )
    guidance = tubing_guidance_message(
        Decimal("3"),
        Decimal("20.00"),
    )

    for body in (variation.body or "", guidance.body or ""):
        normalized = body.casefold()
        assert "valor cadastrado" not in normalized
        assert "inclui até 3 metros" in normalized
        assert "cada metro adicional custa r$ 20,00" in normalized
        assert "entra no orçamento" in normalized



def test_media_received_copy_respects_portuguese_gender() -> None:
    expected = {
        "image": "Recebi a foto. Ela ficará registrada nesta conversa.",
        "video": "Recebi o vídeo. Ele ficará registrado nesta conversa.",
        "audio": "Recebi o áudio. Ele ficará registrado nesta conversa.",
        "document": "Recebi o arquivo. Ele ficará registrado nesta conversa.",
    }

    for kind, body in expected.items():
        assert media_received_message(kind).body == body


def test_unexpected_media_copy_is_grammatical_and_does_not_claim_handoff_early() -> None:
    expected_openers = {
        "image": "Recebi sua foto.",
        "video": "Recebi seu vídeo.",
        "audio": "Recebi seu áudio.",
        "document": "Recebi seu arquivo.",
    }

    for kind, opener in expected_openers.items():
        message = unsupported_media_message(kind)
        body = message.body or ""
        normalized = body.casefold()

        assert body.startswith(opener)
        assert "encaminhei para a equipe" not in normalized
        assert "posso encaminhar o atendimento para a equipe" in normalized
        assert message.interactive_id == "media.unsupported"

from types import SimpleNamespace

import pytest

import app.reengagement.service as reengagement
from app.reengagement.service import campaign_message


def test_upgrade_campaign_uses_three_distinct_light_copy_angles() -> None:
    messages = [campaign_message("upgrade", step) for step in (1, 2, 3)]

    assert len({message.subject for message in messages}) == 3
    assert len({message.body for message in messages}) == 3
    assert all(message.cta_path == "/app/mais/plano" for message in messages)
    assert "cliente não sabe que você está ocupado" in messages[0].title.casefold()
    assert "caixa de entrada" in messages[1].subject.casefold()
    assert "visita técnica" in messages[2].subject.casefold()
    joined = " ".join(message.body.casefold() for message in messages)
    assert "empresa de refrigeração" in joined
    assert "garantido" not in joined
    assert "última chance" not in joined


def test_whatsapp_campaign_keeps_cta_bound_to_the_real_next_action() -> None:
    message = campaign_message(
        "whatsapp_activation",
        2,
        cta_path="/app/whatsapp/business?auto=1",
        action_hint="Falta concluir a autorização oficial da Meta.",
    )

    assert message.cta_path == "/app/whatsapp/business?auto=1"
    assert message.cta_label == "Retomar do ponto certo"
    assert "Falta concluir a autorização oficial da Meta." in message.body


@pytest.mark.asyncio
async def test_paid_user_is_not_nudged_while_meta_review_needs_no_action(
    monkeypatch,
) -> None:
    view = SimpleNamespace(
        status=SimpleNamespace(value="pending"),
        mode=SimpleNamespace(value="coexistence"),
        has_phone_number_id=True,
        meta_review_status=None,
        last_error_code=reengagement.META_ONBOARDING_PENDING,
    )

    class FakeAdministration:
        def __init__(self, _db) -> None:
            pass

        async def get_connection(self, _business_id):
            return view

    monkeypatch.setattr(
        reengagement,
        "WhatsAppConnectionAdministrationService",
        FakeAdministration,
    )
    service = reengagement.ReengagementService(object())
    membership = SimpleNamespace(
        access_mode="paid",
        business_id="business",
    )

    assert await service._eligible(membership) is None


@pytest.mark.asyncio
async def test_rejected_meta_review_routes_to_guidance_not_restart(
    monkeypatch,
) -> None:
    view = SimpleNamespace(
        status=SimpleNamespace(value="pending"),
        mode=SimpleNamespace(value="coexistence"),
        has_phone_number_id=True,
        meta_review_status="rejected",
        last_error_code=reengagement.META_ONBOARDING_PENDING,
    )

    class FakeAdministration:
        def __init__(self, _db) -> None:
            pass

        async def get_connection(self, _business_id):
            return view

    monkeypatch.setattr(
        reengagement,
        "WhatsAppConnectionAdministrationService",
        FakeAdministration,
    )
    service = reengagement.ReengagementService(object())
    membership = SimpleNamespace(
        access_mode="paid",
        business_id="business",
    )

    campaign, path, hint = await service._eligible(membership)
    assert campaign == "whatsapp_activation"
    assert path == "/app/whatsapp"
    assert "Não reconecte" in hint

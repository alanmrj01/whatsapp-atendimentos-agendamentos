from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.auth.dependencies import require_super_admin
from app.models import Business
from app.platform_admin import service as platform_service
from app.platform_admin.schemas import (
    PlatformBusinessCreateRequest,
    PlatformBusinessListResponse,
    PlatformBusinessResponse,
)
from app.platform_admin.service import PlatformAdminService


def make_payload(**overrides) -> PlatformBusinessCreateRequest:
    values = {
        "name": "Refrigeração Piloto",
        "timezone": "America/Sao_Paulo",
        "owner_email": "owner@example.test",
        "owner_password": "correct-horse-battery",
    }
    values.update(overrides)
    return PlatformBusinessCreateRequest(**values)


def test_platform_business_create_normalizes_safe_fields_and_hides_password() -> None:
    payload = PlatformBusinessCreateRequest(
        name="  Refrigeração   Piloto  ",
        timezone="America/Sao_Paulo",
        owner_email=" OWNER@EXAMPLE.TEST ",
        owner_password="correct-horse-battery",
    )
    assert payload.name == "Refrigeração Piloto"
    assert payload.owner_email == "owner@example.test"
    assert payload.timezone == "America/Sao_Paulo"
    assert "correct-horse-battery" not in repr(payload)


@pytest.mark.parametrize("timezone", ["", "Invalid/Timezone", "America/Sao Paulo"])
def test_platform_business_create_rejects_invalid_timezone(timezone: str) -> None:
    with pytest.raises(ValidationError):
        PlatformBusinessCreateRequest(
            name="Piloto",
            timezone=timezone,
            owner_email="owner@example.test",
            owner_password="correct-horse-battery",
        )


def test_super_admin_guard_is_fail_closed() -> None:
    admin = SimpleNamespace(user=SimpleNamespace(platform_role="super_admin"))
    tenant_user = SimpleNamespace(user=SimpleNamespace(platform_role=None))
    assert require_super_admin(admin) is admin
    with pytest.raises(HTTPException) as error:
        require_super_admin(tenant_user)
    assert error.value.status_code == 403


def test_platform_business_create_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PlatformBusinessCreateRequest(
            name="Piloto",
            owner_email="owner@example.test",
            owner_password="correct-horse-battery",
            unexpected="value",
        )


@pytest.mark.asyncio
async def test_create_business_replays_same_completed_operation() -> None:
    payload = make_payload()
    operation_id = uuid4()
    business = Business(
        id=operation_id,
        name=payload.name,
        timezone=payload.timezone,
        active=True,
    )
    expected = PlatformBusinessResponse(
        id=business.id,
        name=business.name,
        timezone=business.timezone,
        active=True,
        owners=[payload.owner_email],
        whatsapp_status="disconnected",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=business))
    service = PlatformAdminService(db)
    service.list_businesses = AsyncMock(
        return_value=PlatformBusinessListResponse(businesses=[expected])
    )

    assert await service.create_business(payload, operation_id) == expected
    service.list_businesses.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_business_rejects_reused_key_for_different_request() -> None:
    payload = make_payload()
    operation_id = uuid4()
    business = Business(
        id=operation_id,
        name="Outra empresa",
        timezone=payload.timezone,
        active=True,
    )
    existing = PlatformBusinessResponse(
        id=business.id,
        name=business.name,
        timezone=business.timezone,
        active=True,
        owners=[payload.owner_email],
        whatsapp_status="disconnected",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=business))
    service = PlatformAdminService(db)
    service.list_businesses = AsyncMock(
        return_value=PlatformBusinessListResponse(businesses=[existing])
    )

    with pytest.raises(HTTPException) as error:
        await service.create_business(payload, operation_id)
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_create_business_uses_operation_uuid_as_business_id(monkeypatch) -> None:
    payload = make_payload()
    operation_id = uuid4()
    db = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=None),
        add_all=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    async def fake_run_sync(*_args, **_kwargs):
        return "$argon2id$test"

    monkeypatch.setattr(platform_service.to_thread, "run_sync", fake_run_sync)
    result = await PlatformAdminService(db).create_business(payload, operation_id)

    assert result.id == operation_id
    added = db.add_all.call_args.args[0]
    created_business = next(item for item in added if isinstance(item, Business))
    assert created_business.id == operation_id
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_business_without_header_stays_backward_compatible(monkeypatch) -> None:
    payload = make_payload()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        add_all=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    async def fake_run_sync(*_args, **_kwargs):
        return "$argon2id$test"

    monkeypatch.setattr(platform_service.to_thread, "run_sync", fake_run_sync)
    result = await PlatformAdminService(db).create_business(payload)

    assert isinstance(result.id, UUID)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_business_real_owner_email_conflict_is_409() -> None:
    payload = make_payload()
    operation_id = uuid4()
    db = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=object()),
    )

    with pytest.raises(HTTPException) as error:
        await PlatformAdminService(db).create_business(payload, operation_id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_create_business_recovers_concurrent_or_lost_response_retry(monkeypatch) -> None:
    payload = make_payload()
    operation_id = uuid4()
    business = Business(
        id=operation_id,
        name=payload.name,
        timezone=payload.timezone,
        active=True,
    )
    expected = PlatformBusinessResponse(
        id=business.id,
        name=business.name,
        timezone=business.timezone,
        active=True,
        owners=[payload.owner_email],
        whatsapp_status="disconnected",
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[None, business]),
        scalar=AsyncMock(return_value=None),
        add_all=Mock(),
        commit=AsyncMock(side_effect=IntegrityError("insert", {}, Exception("race"))),
        rollback=AsyncMock(),
    )

    async def fake_run_sync(*_args, **_kwargs):
        return "$argon2id$test"

    monkeypatch.setattr(platform_service.to_thread, "run_sync", fake_run_sync)
    service = PlatformAdminService(db)
    service.list_businesses = AsyncMock(
        return_value=PlatformBusinessListResponse(businesses=[expected])
    )

    assert await service.create_business(payload, operation_id) == expected
    db.rollback.assert_awaited_once()

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth.dependencies import require_super_admin
from app.platform_admin.schemas import PlatformBusinessCreateRequest


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

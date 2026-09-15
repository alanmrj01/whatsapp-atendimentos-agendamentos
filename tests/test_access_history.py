from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.operational_pwa import AGENDA_ROLES, _authorize, _membership
from app.auth.schemas import MembershipResponse, MembershipRole
from app.models import Business, BusinessAccess
from app.platform_admin.service import PlatformAdminService


def principal_with(*, access_mode: str, history: bool, role=MembershipRole.OWNER):
    membership = MembershipResponse(
        business_id=uuid4(),
        business_name="Empresa",
        role=role,
        access_mode=access_mode,
        has_had_operational_access=history,
    )
    return SimpleNamespace(active_membership=lambda: membership), membership


def test_never_activated_free_tenant_cannot_read_real_operational_data() -> None:
    principal, _ = principal_with(access_mode="free", history=False)
    with pytest.raises(HTTPException) as error:
        _membership(principal)
    assert error.value.status_code == 402


def test_revoked_tenant_keeps_read_access_but_not_mutation_access() -> None:
    principal, membership = principal_with(access_mode="free", history=True)
    assert _membership(principal) == membership

    with pytest.raises(HTTPException) as error:
        _authorize(principal, AGENDA_ROLES)
    assert error.value.status_code == 402


def test_active_paid_tenant_keeps_existing_operational_authorization() -> None:
    principal, membership = principal_with(access_mode="paid", history=True)
    assert _membership(principal) == membership
    assert _authorize(principal, AGENDA_ROLES) == membership


@pytest.mark.asyncio
async def test_admin_grant_records_operational_history_without_billing() -> None:
    business = Business(id=uuid4(), name="Empresa", active=True)
    result = SimpleNamespace(one=Mock(return_value=(business.id, "paid")))
    db = SimpleNamespace(
        get=AsyncMock(return_value=business),
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
    )

    response = await PlatformAdminService(db).set_business_access(business.id, "paid")

    assert response.access_mode == "paid"
    statement = db.execute.await_args.args[0]
    compiled = statement.compile()
    assert statement.table.name == BusinessAccess.__tablename__
    assert "has_had_operational_access" in str(compiled)
    assert True in compiled.params.values()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_revoke_updates_entitlement_without_delete_or_data_reset() -> None:
    business = Business(id=uuid4(), name="Empresa", active=True)
    result = SimpleNamespace(one=Mock(return_value=(business.id, "free")))
    db = SimpleNamespace(
        get=AsyncMock(return_value=business),
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        delete=AsyncMock(),
    )

    response = await PlatformAdminService(db).set_business_access(business.id, "free")

    assert response.access_mode == "free"
    statement = db.execute.await_args.args[0]
    assert "ON CONFLICT" in str(statement)
    assert "has_had_operational_access" in str(statement)
    db.delete.assert_not_awaited()
    db.commit.assert_awaited_once()

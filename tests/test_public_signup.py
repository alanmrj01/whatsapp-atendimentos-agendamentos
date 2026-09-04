from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth import service as auth_service
from app.auth.schemas import SignupRequest
from app.auth.service import AuthService
from app.models import AuthSession, Business, BusinessAccess, BusinessUserMembership, User


def make_payload(**overrides) -> SignupRequest:
    values = {
        "business_name": "João Refrigeração",
        "email": "joao@example.test",
        "password": "correct-horse-battery",
    }
    values.update(overrides)
    return SignupRequest(**values)


def test_signup_request_normalizes_fields_and_hides_password() -> None:
    payload = SignupRequest(
        business_name="  João   Refrigeração  ",
        email=" JOAO@EXAMPLE.TEST ",
        password="correct-horse-battery",
    )
    assert payload.business_name == "João Refrigeração"
    assert payload.email == "joao@example.test"
    assert "correct-horse-battery" not in repr(payload)


def test_signup_request_rejects_short_password_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            business_name="Piloto",
            email="owner@example.test",
            password="curta",
        )
    with pytest.raises(ValidationError):
        SignupRequest(
            business_name="Piloto",
            email="owner@example.test",
            password="correct-horse-battery",
            unexpected="value",
        )


@pytest.mark.asyncio
async def test_signup_creates_free_business_owner_and_active_session(monkeypatch) -> None:
    payload = make_payload()
    operation_id = uuid4()
    db = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=None),
        add_all=Mock(),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    async def fake_run_sync(function, *args):
        if function is auth_service.hash_password:
            return "$argon2id$test"
        return function(*args)

    monkeypatch.setattr(auth_service.to_thread, "run_sync", fake_run_sync)
    user, session, refresh = await AuthService(db).signup(payload, operation_id)

    parents = db.add_all.call_args_list[0].args[0]
    children = db.add_all.call_args_list[1].args[0]
    business = next(item for item in parents if isinstance(item, Business))
    created_user = next(item for item in parents if isinstance(item, User))
    access = next(item for item in children if isinstance(item, BusinessAccess))
    membership = next(item for item in children if isinstance(item, BusinessUserMembership))

    assert business.id == operation_id
    assert business.name == payload.business_name
    assert created_user is user
    assert access.business_id == business.id
    assert access.access_mode == "free"
    assert membership.user_id == user.id
    assert membership.business_id == business.id
    assert membership.role == "owner"
    assert isinstance(session, AuthSession)
    assert session.user_id == user.id
    assert session.active_business_id == business.id
    assert refresh
    assert db.flush.await_count == 2
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_signup_rejects_existing_email() -> None:
    payload = make_payload()
    db = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=object()),
    )

    with pytest.raises(HTTPException) as error:
        await AuthService(db).signup(payload, uuid4())
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_signup_replays_lost_response_with_same_key_and_credentials(monkeypatch) -> None:
    payload = make_payload()
    operation_id = uuid4()
    business = Business(
        id=operation_id,
        name=payload.business_name,
        timezone="America/Sao_Paulo",
        active=True,
    )
    user = User(
        id=uuid4(),
        email=payload.email,
        password_hash="$argon2id$test",
        is_active=True,
        platform_role=None,
    )
    membership = BusinessUserMembership(
        user_id=user.id,
        business_id=business.id,
        role="owner",
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[business, user]),
        scalar=AsyncMock(return_value=membership),
        add=Mock(),
        commit=AsyncMock(),
    )

    async def fake_run_sync(function, *_args):
        if function is auth_service.verify_password:
            return True
        raise AssertionError("unexpected function")

    monkeypatch.setattr(auth_service.to_thread, "run_sync", fake_run_sync)
    replay_user, session, refresh = await AuthService(db).signup(payload, operation_id)

    assert replay_user is user
    assert session.active_business_id == business.id
    assert refresh
    db.add.assert_called_once_with(session)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_signup_rejects_reused_key_with_different_credentials(monkeypatch) -> None:
    payload = make_payload()
    operation_id = uuid4()
    business = Business(
        id=operation_id,
        name=payload.business_name,
        timezone="America/Sao_Paulo",
        active=True,
    )
    user = User(
        id=uuid4(),
        email=payload.email,
        password_hash="$argon2id$test",
        is_active=True,
        platform_role=None,
    )
    membership = BusinessUserMembership(
        user_id=user.id,
        business_id=business.id,
        role="owner",
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[business, user]),
        scalar=AsyncMock(return_value=membership),
        add=Mock(),
        commit=AsyncMock(),
    )

    async def fake_run_sync(function, *_args):
        if function is auth_service.verify_password:
            return False
        raise AssertionError("unexpected function")

    monkeypatch.setattr(auth_service.to_thread, "run_sync", fake_run_sync)
    with pytest.raises(HTTPException) as error:
        await AuthService(db).signup(payload, operation_id)
    assert error.value.status_code == 400
    db.add.assert_not_called()
    db.commit.assert_not_awaited()

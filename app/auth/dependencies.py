from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access
from app.auth.service import AuthService, Principal, unauthorized
from app.core.config import Settings, get_settings
from app.core.database import get_db


def require_auth_config(settings: Annotated[Settings, Depends(get_settings)]) -> Settings:
    secret = settings.auth_jwt_secret
    if not secret or len(secret.get_secret_value().encode()) < 32 or not settings.allowed_pwa_origins():
        raise HTTPException(503, "Authentication unavailable")
    return settings


def require_origin(request: Request, settings: Annotated[Settings, Depends(require_auth_config)]) -> None:
    if request.headers.get("origin") not in settings.allowed_pwa_origins():
        raise HTTPException(403, "Origin not allowed")


async def require_principal(
    request: Request,
    settings: Annotated[Settings, Depends(require_auth_config)],
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 4096:
        raise unauthorized()
    try:
        user_id, session_id = decode_access(authorization[7:], settings.auth_jwt_secret.get_secret_value())
    except ValueError:
        raise unauthorized() from None
    if request.query_params:
        raise HTTPException(400, "Query parameters are not supported")
    return await AuthService(db).authenticate(user_id, session_id)

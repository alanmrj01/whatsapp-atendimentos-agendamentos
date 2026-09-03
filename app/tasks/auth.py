from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

from app.core.config import (
    CloudTasksConfiguration,
    CloudTasksConfigurationError,
    Settings,
    get_settings,
)


def _verify_google_oidc_token(token: str, audience: str) -> dict[str, Any]:
    return id_token.verify_oauth2_token(
        token,
        GoogleAuthRequest(),
        audience=audience,
    )


async def require_cloud_tasks_oidc(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    try:
        configuration = settings.require_cloud_tasks_configuration()
    except CloudTasksConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task processing unavailable",
        ) from None

    await _require_oidc_identity(configuration, authorization)


async def require_outbound_tasks_oidc(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    try:
        configuration = settings.require_outbound_tasks_configuration()
    except CloudTasksConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task processing unavailable",
        ) from None

    await _require_oidc_identity(configuration, authorization)


async def _require_oidc_identity(
    configuration: CloudTasksConfiguration,
    authorization: str | None,
) -> None:
    await require_service_oidc_identity(
        configuration.oidc_audience, configuration.invoker_email, authorization,
        unauthorized_detail="Unauthorized task request",
        forbidden_detail="Forbidden task identity",
    )


async def require_service_oidc_identity(
    audience: str,
    invoker_email: str,
    authorization: str | None,
    *,
    unauthorized_detail: str = "Unauthorized service request",
    forbidden_detail: str = "Forbidden service identity",
) -> None:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=unauthorized_detail,
        )
    scheme, separator, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=unauthorized_detail,
        )

    try:
        claims = await asyncio.to_thread(
            _verify_google_oidc_token, token.strip(), audience,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=unauthorized_detail,
        ) from None

    token_email = claims.get("email")
    if (
        not isinstance(token_email, str)
        or claims.get("email_verified") is not True
        or not secrets.compare_digest(
            token_email.casefold(),
            invoker_email.casefold(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
        )

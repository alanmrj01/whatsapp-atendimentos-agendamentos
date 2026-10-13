from __future__ import annotations

import asyncio
import re
import secrets
from typing import Annotated, Any
from urllib.parse import urlsplit

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


async def require_scheduled_job_oidc(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Authenticate Scheduler-style HTTP jobs without enabling Cloud Tasks queues."""

    scheme, separator, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized scheduled job request",
        )

    audience = (settings.cloud_tasks_oidc_audience or "").strip()
    invoker_email = (settings.cloud_tasks_invoker_email or "").strip()
    try:
        parsed = urlsplit(audience)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", invoker_email)
            is not None
        )
    except ValueError:
        valid = False

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduled job authorization unavailable",
        )

    await require_service_oidc_identity(
        audience,
        invoker_email,
        authorization,
        unauthorized_detail="Unauthorized scheduled job request",
        forbidden_detail="Forbidden scheduled job identity",
    )


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

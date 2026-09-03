from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request

from app.core.config import Settings, get_settings
from app.core.database import get_engine
from app.diagnostics.models import EssentialComponents
from app.diagnostics.repository import DiagnosticsRepository
from app.diagnostics.service import DiagnosticsService
from app.tasks.auth import require_service_oidc_identity


def get_diagnostics_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DiagnosticsService:
    return DiagnosticsService(DiagnosticsRepository(get_engine), settings)


async def get_readiness(
    request: Request,
    service: Annotated[DiagnosticsService, Depends(get_diagnostics_service)],
) -> EssentialComponents:
    return await service.readiness(bool(getattr(request.app.state, "initialized", False)))


async def require_diagnostics_oidc(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    # Anonymous requests are always 401, even if identity config is unavailable.
    scheme, separator, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token.strip():
        raise HTTPException(status_code=401, detail="Unauthorized service request")
    # An explicit pair overrides the task identity. Never mix a partial pair
    # with fallback values, and never depend on queue feature flags for access.
    audience, email = settings.diagnostics_oidc_audience, settings.diagnostics_invoker_email
    if audience is None and email is None:
        audience, email = settings.cloud_tasks_oidc_audience, settings.cloud_tasks_invoker_email
    try:
        parsed = urlsplit(audience or "")
        valid = (
            parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and email is not None
            and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=503, detail="Diagnostics authorization unavailable")
    await require_service_oidc_identity(audience, email, authorization)

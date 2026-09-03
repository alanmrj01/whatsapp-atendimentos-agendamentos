from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.core.database import check_database_connection
from app.diagnostics.dependencies import get_readiness
from app.diagnostics.models import EssentialComponents
from app.schemas.health import (
    DatabaseHealthResponse,
    HealthResponse,
    ReadinessResponse,
)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    components: Annotated[EssentialComponents, Depends(get_readiness)],
) -> ReadinessResponse | JSONResponse:
    if not components.ready:
        payload = ReadinessResponse(
            status="not_ready",
            database="connected" if components.database.details.reachable else "unavailable",
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(),
        )
    return ReadinessResponse(status="ready", database="connected")


@router.get(
    "/health/db",
    response_model=DatabaseHealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": DatabaseHealthResponse}},
)
async def database_health(
    is_connected: Annotated[bool, Depends(check_database_connection)],
) -> DatabaseHealthResponse | JSONResponse:
    if not is_connected:
        payload = DatabaseHealthResponse(status="error", database="unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload.model_dump(),
        )
    return DatabaseHealthResponse(status="ok", database="connected")

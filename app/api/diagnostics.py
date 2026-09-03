from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.diagnostics.dependencies import get_diagnostics_service, require_diagnostics_oidc
from app.diagnostics.models import DiagnosticsResponse
from app.diagnostics.service import DiagnosticsService

router = APIRouter(prefix="/internal", tags=["internal-diagnostics"])


@router.get(
    "/diagnostics", response_model=DiagnosticsResponse,
    dependencies=[Depends(require_diagnostics_oidc)],
)
async def diagnostics(
    request: Request,
    response: Response,
    service: Annotated[DiagnosticsService, Depends(get_diagnostics_service)],
) -> DiagnosticsResponse:
    response.headers["Cache-Control"] = "no-store"
    # Authenticated diagnostic retrieval succeeds even when component health is
    # degraded/error. Consumers use the versioned body; /ready drives probes.
    return await service.diagnostics(bool(getattr(request.app.state, "initialized", False)))

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_origin, require_principal
from app.auth.service import Principal
from app.core.database import get_db
from app.reengagement.schemas import ReengagementPromptResponse
from app.reengagement.service import ReengagementService

router = APIRouter(prefix="/api/v1/reengagement", tags=["reengagement"])
Db = Annotated[AsyncSession, Depends(get_db)]
Identity = Annotated[Principal, Depends(require_principal)]


@router.post(
    "/claim",
    response_model=ReengagementPromptResponse | None,
    dependencies=[Depends(require_origin)],
)
async def claim_reengagement_prompt(
    principal: Identity,
    db: Db,
) -> ReengagementPromptResponse | None:
    membership = principal.active_membership()
    return await ReengagementService(db).claim_prompt(
        user_id=principal.user.id,
        membership=membership,
    )


@router.post(
    "/{delivery_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def dismiss_reengagement_prompt(
    delivery_id: UUID,
    principal: Identity,
    db: Db,
) -> Response:
    membership = principal.active_membership()
    await ReengagementService(db).record_interaction(
        delivery_id=delivery_id,
        user_id=principal.user.id,
        business_id=membership.business_id,
        action="dismiss",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{delivery_id}/cta",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_origin)],
)
async def click_reengagement_cta(
    delivery_id: UUID,
    principal: Identity,
    db: Db,
) -> Response:
    membership = principal.active_membership()
    await ReengagementService(db).record_interaction(
        delivery_id=delivery_id,
        user_id=principal.user.id,
        business_id=membership.business_id,
        action="cta",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

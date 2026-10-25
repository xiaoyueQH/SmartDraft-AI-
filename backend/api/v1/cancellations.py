from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.api.v1.auth import AdminDep, PrincipalDep
from backend.db.cancellation_store import CancellationStore
from backend.dependencies import DatabaseDep
from backend.models.cancellation import (
    CancellationCommit,
    CancellationPreview,
    CancellationReason,
    CancellationReceipt,
)
from backend.services.cancellations import CancellationService

router = APIRouter(prefix="/orders", tags=["cancellations"])
admin_router = APIRouter(prefix="/admin/orders", tags=["admin-cancellations"])


def cancellation_service(database: DatabaseDep) -> CancellationService:
    return CancellationService(CancellationStore(database))


CancelDep = Annotated[CancellationService, Depends(cancellation_service)]


@router.post("/{order_id}/cancellation-preview", response_model=CancellationPreview)
async def preview(
    order_id: UUID, body: CancellationReason, principal: PrincipalDep, service: CancelDep
):
    return await service.preview(principal, order_id, body.reason)


@router.post("/{order_id}/cancel", response_model=CancellationReceipt)
async def confirm(
    order_id: UUID, body: CancellationCommit, principal: PrincipalDep, service: CancelDep
):
    return await service.confirm(principal, order_id, body.confirmation_id, body.confirmed)


@admin_router.post("/{order_id}/cancellation-preview", response_model=CancellationPreview)
async def admin_preview(
    order_id: UUID, body: CancellationReason, principal: AdminDep, service: CancelDep
):
    return await service.preview(principal, order_id, body.reason, all_users=True)


@admin_router.post("/{order_id}/cancel", response_model=CancellationReceipt)
async def admin_confirm(
    order_id: UUID, body: CancellationCommit, principal: AdminDep, service: CancelDep
):
    return await service.confirm(
        principal, order_id, body.confirmation_id, body.confirmed, all_users=True
    )

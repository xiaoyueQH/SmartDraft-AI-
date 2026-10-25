from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from backend.api.v1.auth import AdminDep, PrincipalDep
from backend.db.workflow_store import WorkflowStore
from backend.dependencies import DatabaseDep
from backend.models.workflow import (
    Confirmation,
    ReturnCommand,
    ReturnPage,
    ReturnStatus,
    ReviewCommand,
    ShipmentCommand,
    WorkflowReceipt,
)
from backend.services.workflow import WorkflowService

router = APIRouter(tags=["order-workflow"])


def workflow_service(database: DatabaseDep) -> WorkflowService:
    return WorkflowService(WorkflowStore(database))


WorkflowDep = Annotated[WorkflowService, Depends(workflow_service)]


@router.post("/orders/{order_id}/pay", response_model=WorkflowReceipt)
async def pay(order_id: UUID, body: Confirmation, principal: PrincipalDep, service: WorkflowDep):
    return await service.run(principal, order_id, "pay", body)


@router.post("/admin/orders/{order_id}/ship", response_model=WorkflowReceipt)
async def ship(order_id: UUID, body: ShipmentCommand, principal: AdminDep, service: WorkflowDep):
    return await service.run(principal, order_id, "ship", body)


@router.post("/orders/{order_id}/receive", response_model=WorkflowReceipt)
async def receive(
    order_id: UUID, body: Confirmation, principal: PrincipalDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "receive", body)


@router.post("/orders/{order_id}/returns", response_model=WorkflowReceipt)
async def request_return(
    order_id: UUID, body: ReturnCommand, principal: PrincipalDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "return_request", body)


@router.post("/admin/orders/{order_id}/returns/review", response_model=WorkflowReceipt)
async def review_return(
    order_id: UUID, body: ReviewCommand, principal: AdminDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "return_review", body)


@router.post("/orders/{order_id}/returns/ship", response_model=WorkflowReceipt)
async def return_ship(
    order_id: UUID, body: ShipmentCommand, principal: PrincipalDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "return_ship", body)


@router.post("/admin/orders/{order_id}/returns/receive", response_model=WorkflowReceipt)
async def inspect_return(
    order_id: UUID, body: Confirmation, principal: AdminDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "return_receive", body)


@router.post("/admin/orders/{order_id}/returns/refund", response_model=WorkflowReceipt)
async def refund_return(
    order_id: UUID, body: Confirmation, principal: AdminDep, service: WorkflowDep
):
    return await service.run(principal, order_id, "return_refund", body)


@router.get("/returns", response_model=ReturnPage)
async def my_returns(
    principal: PrincipalDep,
    service: WorkflowDep,
    status: ReturnStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    return await service.returns(principal, status, limit, offset)


@router.get("/admin/returns", response_model=ReturnPage)
async def all_returns(
    principal: AdminDep,
    service: WorkflowDep,
    status: ReturnStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    return await service.returns(principal, status, limit, offset, admin=True)

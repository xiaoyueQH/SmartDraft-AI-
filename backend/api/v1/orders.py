from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from backend.api.v1.auth import AdminDep, PrincipalDep
from backend.db.order_store import OrderStore
from backend.dependencies import DatabaseDep
from backend.models.orders import OrderDetail, OrderPage, OrderStatus, ProductPage
from backend.services.orders import OrderService

router = APIRouter(tags=["orders"])
admin_router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


def order_service(database: DatabaseDep) -> OrderService:
    return OrderService(OrderStore(database))


OrderDep = Annotated[OrderService, Depends(order_service)]


class OrderQuery(BaseModel):
    status: OrderStatus | None = None
    number: str | None = Field(default=None, min_length=1, max_length=40)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)


@router.get("/products", response_model=ProductPage)
async def products(
    principal: PrincipalDep,
    service: OrderDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
):
    return await service.store.products(limit, offset)


@router.get("/orders", response_model=OrderPage)
async def my_orders(
    principal: PrincipalDep, service: OrderDep, query: Annotated[OrderQuery, Query()]
):
    return await service.list_orders(principal, **query.model_dump())


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def my_order(order_id: UUID, principal: PrincipalDep, service: OrderDep):
    return await service.detail(principal, order_id)


@admin_router.get("", response_model=OrderPage)
async def all_orders(principal: AdminDep, service: OrderDep, query: Annotated[OrderQuery, Query()]):
    return await service.list_orders(principal, all_users=True, **query.model_dump())


@admin_router.get("/{order_id}", response_model=OrderDetail)
async def admin_order(order_id: UUID, principal: AdminDep, service: OrderDep):
    return await service.detail(principal, order_id, all_users=True)

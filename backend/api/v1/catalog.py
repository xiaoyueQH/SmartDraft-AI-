from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from backend.api.v1.auth import AdminDep
from backend.db.catalog_store import CatalogStore
from backend.dependencies import DatabaseDep
from backend.models.catalog import (
    CandidatePage,
    EditProduct,
    ImportProduct,
    ImportResult,
    ManualProduct,
    ProductMutation,
    ProductState,
)
from backend.models.orders import ProductPage, ProductView
from backend.services.catalog import CatalogService

router = APIRouter(prefix="/admin/catalog", tags=["admin-catalog"])


def catalog_service(database: DatabaseDep) -> CatalogService:
    return CatalogService(CatalogStore(database))


CatalogDep = Annotated[CatalogService, Depends(catalog_service)]


class CatalogQuery(BaseModel):
    q: str = Field(default="", max_length=80)
    category: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=12, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)


@router.get("/candidates", response_model=CandidatePage)
async def candidates(
    principal: AdminDep, service: CatalogDep, query: Annotated[CatalogQuery, Query()]
):
    return await service.candidates(principal, query.q, query.category, query.limit, query.offset)


@router.post("/imports", response_model=ImportResult)
async def import_product(data: ImportProduct, principal: AdminDep, service: CatalogDep):
    return await service.import_product(principal, data)


@router.get("/products", response_model=ProductPage)
async def products(
    principal: AdminDep,
    service: CatalogDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    deleted: bool = False,
):
    return await service.products(principal, limit, offset, deleted=deleted)


@router.post("/products", response_model=ImportResult)
async def create_manual_product(data: ManualProduct, principal: AdminDep, service: CatalogDep):
    return await service.create_manual(principal, data)


@router.patch("/products/{product_id}", response_model=ProductView)
async def edit_product(
    product_id: UUID, data: EditProduct, principal: AdminDep, service: CatalogDep
):
    return await service.mutate(principal, product_id, "edit", data)


@router.post("/products/{product_id}/state", response_model=ProductView)
async def product_state(
    product_id: UUID, data: ProductState, principal: AdminDep, service: CatalogDep
):
    return await service.mutate(principal, product_id, "state", data)


@router.delete("/products/{product_id}", response_model=ProductView)
async def delete_product(
    product_id: UUID, data: ProductMutation, principal: AdminDep, service: CatalogDep
):
    return await service.mutate(principal, product_id, "delete", data)


@router.post("/products/{product_id}/restore", response_model=ProductView)
async def restore_product(
    product_id: UUID, data: ProductMutation, principal: AdminDep, service: CatalogDep
):
    return await service.mutate(principal, product_id, "restore", data)

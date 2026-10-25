from uuid import UUID

from backend.db.catalog_store import CatalogStore
from backend.models.auth import AuthError, Principal
from backend.models.catalog import (
    CandidateNotFound,
    CandidatePage,
    ImportProduct,
    ImportResult,
    ManualProduct,
    ProductAction,
    ProductMutation,
)
from backend.models.orders import ProductPage, ProductView
from backend.services.mock_catalog import CATALOG, search_candidates


class CatalogService:
    def __init__(self, store: CatalogStore):
        self.store = store

    async def create_manual(self, principal: Principal, data: ManualProduct) -> ImportResult:
        self.require_admin(principal)
        return await self.store.create_manual(principal, data)

    @staticmethod
    def require_admin(principal: Principal) -> None:
        if principal.account.role != "admin":
            raise AuthError("FORBIDDEN", "需要管理员权限", 403)

    async def candidates(
        self, principal: Principal, query: str, category: str | None, limit: int, offset: int
    ) -> CandidatePage:
        self.require_admin(principal)
        matches = search_candidates(query, category)
        selected = matches[offset : offset + limit]
        imported = await self.store.imported_ids([item.id for item in selected])
        return CandidatePage(
            items=[
                item.model_copy(update={"imported_product_id": imported.get(item.id)})
                for item in selected
            ],
            categories=list(dict.fromkeys(item.category for item in CATALOG)),
            has_more=len(matches) > offset + limit,
        )

    async def products(
        self, principal: Principal, limit: int, offset: int, *, deleted: bool = False
    ) -> ProductPage:
        self.require_admin(principal)
        return await self.store.products(limit, offset, admin=True, deleted=deleted)

    async def mutate(
        self, principal: Principal, product_id: UUID, action: ProductAction, data: ProductMutation
    ) -> ProductView:
        self.require_admin(principal)
        return await self.store.mutate(principal, product_id, action, data)

    async def import_product(self, principal: Principal, data: ImportProduct) -> ImportResult:
        self.require_admin(principal)
        candidate = next((item for item in CATALOG if item.id == data.candidate_id), None)
        if candidate is None:
            raise CandidateNotFound
        return await self.store.import_product(principal, candidate, data)

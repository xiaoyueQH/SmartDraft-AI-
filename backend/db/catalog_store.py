import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.db.identity import lock_identity
from backend.db.session import Database
from backend.models.auth import Principal
from backend.models.catalog import (
    Candidate,
    EditProduct,
    ImportProduct,
    ImportResult,
    ManualProduct,
    ProductAction,
    ProductMutation,
    ProductState,
)
from backend.models.commerce import CatalogImport, Product, ProductChange
from backend.models.commerce_errors import CommerceError
from backend.models.orders import ProductPage, ProductView


class CatalogStore:
    def __init__(self, database: Database):
        self.database = database

    async def create_manual(self, principal: Principal, data: ManualProduct) -> ImportResult:
        payload = data.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        source_id = str(
            uuid5(NAMESPACE_URL, f"supportflow:manual:{principal.account.id}:{data.request_id}")
        )
        product_id = uuid5(NAMESPACE_URL, f"supportflow:manual-product:{source_id}")
        async with self.database.session() as session:
            actor = await lock_identity(session, principal, admin=True, shared=True)

            async def existing_result():
                audit = await session.get(CatalogImport, ("manual", source_id))
                if audit is None:
                    return None
                if audit.candidate_snapshot.get("fingerprint") != fingerprint:
                    raise CommerceError(
                        "REQUEST_REUSED", "该入库请求已用于其他内容，请重新打开新增商品"
                    )
                product = await session.get(Product, audit.product_id)
                if product.deleted_at:
                    raise CommerceError("PRODUCT_DELETED", "商品已在回收站，请先恢复")
                return ImportResult(product=ProductView.model_validate(product), created=False)

            if existing := await existing_result():
                return existing
            product = Product(
                id=product_id,
                sku=data.sku,
                name=data.name,
                description=data.description,
                price_cents=data.price_cents,
                stock=data.stock,
                is_demo=False,
            )
            try:
                async with session.begin_nested():
                    session.add(product)
                    await session.flush()
                    session.add(
                        CatalogImport(
                            source="manual",
                            source_id=source_id,
                            product_id=product.id,
                            actor_id=actor.id,
                            created_at=datetime.now(UTC),
                            candidate_snapshot={"fingerprint": fingerprint, "input": payload},
                        )
                    )
                    await session.flush()
            except IntegrityError:
                if existing := await existing_result():
                    return existing
                if await session.scalar(select(Product.id).where(Product.sku == data.sku)):
                    raise CommerceError(
                        "SKU_EXISTS", "该 SKU 已存在，请检查商品目录或回收站，也可填写其他 SKU"
                    ) from None
                raise
            return ImportResult(product=ProductView.model_validate(product), created=True)

    async def products(
        self, limit: int, offset: int, *, admin: bool = False, deleted: bool = False
    ) -> ProductPage:
        statement = select(Product).where(
            Product.deleted_at.is_not(None) if deleted else Product.deleted_at.is_(None)
        )
        if not admin:
            statement = statement.where(Product.is_active.is_(True), Product.deleted_at.is_(None))
        async with self.database.session() as session:
            rows = list(
                await session.scalars(
                    statement.order_by(Product.sku).limit(limit + 1).offset(offset)
                )
            )
            return ProductPage(
                items=[ProductView.model_validate(row) for row in rows[:limit]],
                has_more=len(rows) > limit,
            )

    async def imported_ids(self, ids: list[str]) -> dict:
        if not ids:
            return {}
        async with self.database.session() as session:
            return dict(
                (
                    await session.execute(
                        select(CatalogImport.source_id, CatalogImport.product_id).where(
                            CatalogImport.source == "mock_catalog_v1",
                            CatalogImport.source_id.in_(ids),
                        )
                    )
                ).all()
            )

    async def import_product(
        self, principal: Principal, candidate: Candidate, data: ImportProduct
    ) -> ImportResult:
        async with self.database.session() as session:
            user = await lock_identity(session, principal, admin=True, shared=True)

            existing = (
                select(Product)
                .join(CatalogImport)
                .where(
                    CatalogImport.source == candidate.source,
                    CatalogImport.source_id == candidate.id,
                )
            )
            product = await session.scalar(existing)
            if product:
                if product.deleted_at:
                    raise CommerceError(
                        "PRODUCT_DELETED", "该商品已在回收站，请先恢复，不会重复入库"
                    )
                return ImportResult(product=ProductView.model_validate(product), created=False)
            product = Product(
                id=uuid5(NAMESPACE_URL, f"supportflow:{candidate.source}:{candidate.id}"),
                sku=f"MOCK-{candidate.id}",
                name=data.name,
                description=data.description,
                price_cents=data.price_cents,
                stock=data.stock,
                is_demo=True,
            )
            try:
                async with session.begin_nested():
                    session.add(product)
                    await session.flush()
                    session.add(
                        CatalogImport(
                            source=candidate.source,
                            source_id=candidate.id,
                            product_id=product.id,
                            actor_id=user.id,
                            created_at=datetime.now(UTC),
                            candidate_snapshot=candidate.model_dump(
                                mode="json", exclude={"imported_product_id"}
                            ),
                        )
                    )
                    await session.flush()
            except IntegrityError:
                # 唯一约束处理跨管理员竞争；重试只读取首次结果，不覆盖售价或加库存。
                product = await session.scalar(existing)
                if product is None:
                    raise
                if product.deleted_at:
                    raise CommerceError("PRODUCT_DELETED", "该商品已在回收站，请先恢复") from None
                return ImportResult(product=ProductView.model_validate(product), created=False)
            return ImportResult(product=ProductView.model_validate(product), created=True)

    async def mutate(
        self, principal: Principal, product_id: UUID, action: ProductAction, data: ProductMutation
    ) -> ProductView:
        fingerprint = hashlib.sha256(
            json.dumps({"action": action, **data.model_dump(mode="json")}, sort_keys=True).encode()
        ).hexdigest()
        change_id = uuid5(
            NAMESPACE_URL,
            f"supportflow:product:{principal.account.id}:{product_id}:{data.operation_id}",
        )
        async with self.database.session() as session:
            actor = await lock_identity(session, principal, admin=True, shared=True)
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            if product is None:
                raise CommerceError("PRODUCT_NOT_FOUND", "商品不存在", 404)
            existing = await session.get(ProductChange, change_id)
            if existing:
                if existing.fingerprint != fingerprint:
                    raise CommerceError(
                        "OPERATION_REUSED", "该操作编号已用于其他内容，请重新打开商品"
                    )
                return ProductView.model_validate(existing.result_snapshot)
            if product.version != data.expected_version:
                raise CommerceError("PRODUCT_CHANGED", "商品或库存已被更新，请刷新后重新操作")
            if action == "restore":
                if not product.deleted_at:
                    raise CommerceError("PRODUCT_NOT_DELETED", "商品未在回收站")
                product.deleted_at = None
                product.is_active = False
            else:
                if product.deleted_at:
                    raise CommerceError("PRODUCT_DELETED", "商品已在回收站，请先恢复")
                if action == "delete":
                    product.deleted_at = datetime.now(UTC)
                    product.is_active = False
                elif action == "state" and isinstance(data, ProductState):
                    product.is_active = data.is_active
                elif action == "edit" and isinstance(data, EditProduct):
                    product.name = data.name
                    product.description = data.description
                    product.price_cents = data.price_cents
                    product.stock = data.stock
                else:
                    raise ValueError("Invalid product mutation")
            product.version += 1
            await session.flush()
            result = ProductView.model_validate(product)
            session.add(
                ProductChange(
                    id=change_id,
                    product_id=product.id,
                    actor_id=actor.id,
                    action=action,
                    fingerprint=fingerprint,
                    result_snapshot=result.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            await session.flush()
            return result

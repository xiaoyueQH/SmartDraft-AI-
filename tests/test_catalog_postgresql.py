# ruff: noqa: F811
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.catalog_store import CatalogStore
from backend.models.catalog import ImportProduct
from backend.models.commerce import CatalogImport, Product
from backend.services.catalog import CatalogService

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1",
    reason="Requires local PostgreSQL integration database",
)


@pytest_asyncio.fixture
async def catalog_pg_auth(postgres_auth):
    await postgres_auth.create_user("admin", "Admin", "admin", "Postgres-test-password")
    return postgres_auth


async def test_postgres_parallel_imports_from_multiple_admins_create_one_product(catalog_pg_auth):
    auth = catalog_pg_auth
    await auth.create_user("admin2", "Admin2", "admin", "Postgres-test-password")
    principals = [
        await auth.authenticate((await auth.login(name, "Postgres-test-password")).access_token)
        for name in ("admin", "admin2")
    ]
    database = auth.store.database
    service = CatalogService(CatalogStore(database))
    data = ImportProduct(
        candidate_id="GPU-001",
        name="并发模拟显卡",
        description="模拟",
        price_cents=128000,
        stock=20,
    )
    results = await asyncio.gather(
        *(service.import_product(principals[i % 2], data) for i in range(24))
    )
    assert sum(result.created for result in results) == 1
    assert len({result.product.id for result in results}) == 1
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(CatalogImport)) == 1
        product = await session.scalar(select(Product))
        assert product.stock == 20 and product.price_cents == 128000


async def test_postgres_failed_import_transaction_leaves_no_product_or_audit(catalog_pg_auth):
    auth = catalog_pg_auth
    tokens = await auth.login("admin", "Postgres-test-password")
    principal = await auth.authenticate(tokens.access_token)
    database = auth.store.database
    service = CatalogService(CatalogStore(database))
    # 唯一 SKU 冲突不是同一来源的重试，必须报错并回滚。
    async with database.session() as session:
        session.add(
            Product(
                id=uuid4(),
                sku="MOCK-GPU-001",
                name="占用SKU",
                description="模拟",
                price_cents=10,
                stock=2,
                is_demo=True,
            )
        )
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await service.import_product(
            principal,
            ImportProduct(
                candidate_id="GPU-001",
                name="失败商品",
                description="模拟",
                price_cents=100,
                stock=9,
            ),
        )
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(CatalogImport)) == 0
        assert await session.scalar(select(func.count()).select_from(Product)) == 1

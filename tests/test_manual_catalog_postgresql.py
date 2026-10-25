# ruff: noqa: F811
import asyncio
import os

import pytest
from sqlalchemy import func, select
from test_catalog_postgresql import catalog_pg_auth  # noqa: F401
from test_manual_catalog import manual_payload
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.catalog_store import CatalogStore
from backend.models.catalog import ManualProduct
from backend.models.commerce import CatalogImport, Product
from backend.models.commerce_errors import CommerceError
from backend.services.catalog import CatalogService

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires local PostgreSQL"
)


async def test_parallel_manual_retry_creates_one_product_and_source_record(catalog_pg_auth):
    auth = catalog_pg_auth
    principal = await auth.authenticate(
        (await auth.login("admin", "Postgres-test-password")).access_token
    )
    service = CatalogService(CatalogStore(auth.store.database))
    data = ManualProduct(**manual_payload())
    results = await asyncio.gather(*(service.create_manual(principal, data) for _ in range(12)))
    assert sum(row.created for row in results) == 1
    assert len({row.product.id for row in results}) == 1
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(CatalogImport)) == 1
        assert (await session.scalar(select(Product))).stock == 20


async def test_two_admins_cannot_create_duplicate_sku(catalog_pg_auth):
    auth = catalog_pg_auth
    await auth.create_user("admin2", "Admin2", "admin", "Postgres-test-password")
    principals = [
        await auth.authenticate((await auth.login(name, "Postgres-test-password")).access_token)
        for name in ("admin", "admin2")
    ]
    service = CatalogService(CatalogStore(auth.store.database))
    results = await asyncio.gather(
        *(service.create_manual(who, ManualProduct(**manual_payload())) for who in principals),
        return_exceptions=True,
    )
    assert sum(isinstance(row, CommerceError) and row.code == "SKU_EXISTS" for row in results) == 1
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(CatalogImport)) == 1

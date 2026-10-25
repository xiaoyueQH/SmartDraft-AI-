# ruff: noqa: F811
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_catalog import payload
from test_catalog_postgresql import catalog_pg_auth  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.catalog_store import CatalogStore
from backend.db.checkout_store import CheckoutStore
from backend.models.catalog import EditProduct, ImportProduct, ProductMutation
from backend.models.checkout import CheckoutRequest
from backend.models.commerce import InventoryReservation, Order, Product, ProductChange
from backend.models.commerce_errors import CommerceError
from backend.services.catalog import CatalogService

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires local PostgreSQL"
)


async def setup_product(auth, stock=3):
    admin = await auth.authenticate(
        (await auth.login("admin", "Postgres-test-password")).access_token
    )
    catalog = CatalogService(CatalogStore(auth.store.database))
    result = await catalog.import_product(admin, ImportProduct(**payload(stock=stock)))
    return admin, catalog, result.product


async def test_concurrent_checkout_never_oversells_and_same_request_only_reserves_once(
    catalog_pg_auth,
):
    auth = catalog_pg_auth
    _, _, product = await setup_product(auth)
    users = [
        await auth.authenticate((await auth.login(name, "Postgres-test-password")).access_token)
        for name in ("alice", "bob")
    ]
    store = CheckoutStore(auth.store.database)
    requests = [
        CheckoutRequest(
            request_id=uuid4(),
            product_id=product.id,
            quantity=1,
            expected_price_cents=product.price_cents,
        )
        for _ in range(12)
    ]
    results = await asyncio.gather(
        *(store.create(users[i % 2], request) for i, request in enumerate(requests)),
        return_exceptions=True,
    )
    successes = [
        (i, result) for i, result in enumerate(results) if not isinstance(result, Exception)
    ]
    assert len(successes) == 3
    assert all(
        isinstance(result, CommerceError) and result.code == "INSUFFICIENT_STOCK"
        for result in results
        if isinstance(result, Exception)
    )
    i, first = successes[0]
    replay = await asyncio.gather(*(store.create(users[i % 2], requests[i]) for _ in range(6)))
    assert all(not item.created and item.order.id == first.order.id for item in replay)
    async with auth.store.database.session() as session:
        assert (await session.get(Product, product.id)).stock == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 3
        assert await session.scalar(select(func.sum(InventoryReservation.quantity))) == 3


async def test_two_admin_updates_cannot_silently_overwrite_each_other(catalog_pg_auth):
    auth = catalog_pg_auth
    admin, catalog, product = await setup_product(auth)
    await auth.create_user("admin2", "Second", "admin", "Postgres-test-password")
    second = await auth.authenticate(
        (await auth.login("admin2", "Postgres-test-password")).access_token
    )
    results = await asyncio.gather(
        *(
            catalog.mutate(
                who,
                product.id,
                "edit",
                EditProduct(
                    operation_id=uuid4(),
                    expected_version=product.version,
                    name=f"新版 {index}",
                    description="新说明",
                    price_cents=999,
                    stock=5 + index,
                ),
            )
            for index, who in enumerate((admin, second))
        ),
        return_exceptions=True,
    )
    assert (
        sum(
            isinstance(result, CommerceError) and result.code == "PRODUCT_CHANGED"
            for result in results
        )
        == 1
    )
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProductChange)) == 1
        assert (await session.get(Product, product.id)).version == product.version + 1


async def test_audit_failure_rolls_back_product_deletion(catalog_pg_auth):
    auth = catalog_pg_auth
    admin, catalog, product = await setup_product(auth)

    def fail_audit(session, *_):
        if any(isinstance(row, ProductChange) for row in session.new):
            raise RuntimeError("forced audit failure")

    event.listen(Session, "before_flush", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="forced audit failure"):
            await catalog.mutate(
                admin,
                product.id,
                "delete",
                ProductMutation(operation_id=uuid4(), expected_version=product.version),
            )
    finally:
        event.remove(Session, "before_flush", fail_audit)
    async with auth.store.database.session() as session:
        row = await session.get(Product, product.id)
        assert row.deleted_at is None and row.is_active and row.version == product.version
        assert await session.scalar(select(func.count()).select_from(ProductChange)) == 0

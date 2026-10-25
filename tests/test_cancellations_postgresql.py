import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.api.v1.auth import current_principal
from backend.db.cancellation_store import CancellationStore
from backend.db.order_store import OrderStore
from backend.db.session import Database
from backend.dependencies import get_database
from backend.main import create_app
from backend.models.cancellation import CancellationError
from backend.models.commerce import (
    InventoryReservation,
    Order,
    OrderCancellation,
    OrderItem,
    Product,
    Shipment,
)
from backend.models.fulfilment import OrderEvent, Payment
from backend.services.cancellations import CancellationService
from scripts.reserve_demo_inventory import reserve_demo_inventory
from scripts.seed_demo_orders import seed_orders

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1",
    reason="Requires local PostgreSQL integration database",
)


@pytest_asyncio.fixture
async def cancel_pg(postgres_auth):  # noqa: F811
    auth = postgres_auth
    await auth.create_user("user", "Demo", "user", "Postgres-test-password")
    database = auth.store.database
    await seed_orders(database)
    assert await reserve_demo_inventory(database) == 3
    assert await reserve_demo_inventory(database) == 0
    bundle = await auth.login("user", "Postgres-test-password")
    principal = await auth.authenticate(bundle.access_token)
    async with database.session() as session:
        order = await session.scalar(select(Order).where(Order.status == "pending_payment"))
        item = await session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
        stock = (await session.get(Product, item.product_id)).stock
        order_id, item_id, product_id = order.id, item.id, item.product_id
    service = CancellationService(CancellationStore(database))
    yield service, principal, database, order_id, item_id, product_id, stock, auth


async def test_postgres_duplicate_confirmation_releases_once_under_twelve_concurrent_requests(
    cancel_pg,
):
    service, principal, database, order_id, item_id, product_id, stock, _ = cancel_pg
    quote = await service.preview(principal, order_id, "并发重试")
    results = await asyncio.gather(
        *(service.confirm(principal, order_id, quote.confirmation_id, True) for _ in range(12))
    )
    assert all(result == results[0] for result in results)
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == stock + 1
        assert (await session.get(InventoryReservation, item_id)).released_at is not None
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 1


async def test_historical_demo_payment_backfill_is_idempotent_and_never_changes_stock(cancel_pg):
    _, _, database, _, _, _, _, _ = cancel_pg
    async with database.session() as session:
        before = {p.id: (p.stock, p.version) for p in await session.scalars(select(Product))}
        assert await session.scalar(select(func.count()).select_from(Payment)) == 3
        assert (
            await session.scalar(
                select(func.count()).select_from(OrderEvent).where(OrderEvent.action == "pay")
            )
            == 3
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InventoryReservation)
                .where(InventoryReservation.consumed_at.is_not(None))
            )
            == 2
        )
    assert await reserve_demo_inventory(database) == 0
    async with database.session() as session:
        assert {
            p.id: (p.stock, p.version) for p in await session.scalars(select(Product))
        } == before
        assert await session.scalar(select(func.count()).select_from(Payment)) == 3
        assert (
            await session.scalar(
                select(func.count()).select_from(OrderEvent).where(OrderEvent.action == "pay")
            )
            == 3
        )


async def test_postgres_two_distinct_confirmations_cannot_double_cancel(cancel_pg):
    service, principal, database, order_id, _, product_id, stock, _ = cancel_pg
    first = await service.preview(principal, order_id, "首次确认")
    second = await service.preview(principal, order_id, "另一个窗口")
    results = await asyncio.gather(
        service.confirm(principal, order_id, first.confirmation_id, True),
        service.confirm(principal, order_id, second.confirmation_id, True),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CancellationError) for item in results) == 1
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == stock + 1
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 1


async def test_postgres_cancellations_for_different_users_do_not_lose_shared_stock_updates(
    cancel_pg,
):
    service, principal, database, order_id, _, product_id, stock, auth = cancel_pg
    bob_login = await auth.login("bob", "Postgres-test-password")
    bob = await auth.authenticate(bob_login.access_token)
    bob_order, bob_item = uuid4(), uuid4()
    async with database.session() as session:
        session.add(
            Order(
                id=bob_order,
                number="BOB-SHARED-STOCK",
                user_id=bob.account.id,
                status="pending_payment",
                total_cents=12900,
                created_at=datetime.now(UTC),
                is_demo=True,
            )
        )
        await session.flush()
        session.add(
            OrderItem(
                id=bob_item,
                order_id=bob_order,
                product_id=product_id,
                sku="DEMO-KETTLE",
                product_name="演示商品",
                unit_price_cents=12900,
                quantity=1,
            )
        )
        await session.flush()
        session.add(
            InventoryReservation(order_item_id=bob_item, quantity=1, created_at=datetime.now(UTC))
        )
        (await session.get(Product, product_id)).stock -= 1
    first, second = await asyncio.gather(
        service.preview(principal, order_id, "测试"), service.preview(bob, bob_order, "测试")
    )
    await asyncio.gather(
        service.confirm(principal, order_id, first.confirmation_id, True),
        service.confirm(bob, bob_order, second.confirmation_id, True),
    )
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == stock + 1
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 2


async def test_postgres_shipping_wins_race_and_cancel_rechecks_locked_state(cancel_pg):
    service, principal, database, order_id, _, product_id, stock, _ = cancel_pg
    quote = await service.preview(principal, order_id, "取消与发货竞争")
    async with database.session() as shipping:
        order = await shipping.scalar(select(Order).where(Order.id == order_id).with_for_update())
        order.status = "shipped"
        shipping.add(
            Shipment(order_id=order_id, carrier="模拟", tracking_number="DEMO-RACE", is_demo=True)
        )
        task = asyncio.create_task(
            service.confirm(principal, order_id, quote.confirmation_id, True)
        )
        await asyncio.sleep(0.05)
    with pytest.raises(CancellationError):
        await task
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == stock
        assert (await session.get(Order, order_id)).status == "shipped"
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 0


async def test_postgres_cancelled_request_rolls_back_and_original_confirmation_is_retryable(
    cancel_pg,
):
    service, principal, database, order_id, _, product_id, stock, _ = cancel_pg
    quote = await service.preview(principal, order_id, "中断后重试")
    waiting = asyncio.Event()

    def before_product_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("SELECT") and "products" in statement and "FOR UPDATE" in statement:
            waiting.set()

    async with database.session() as holder:
        await holder.scalar(select(Product).where(Product.id == product_id).with_for_update())
        event.listen(database.engine.sync_engine, "before_cursor_execute", before_product_lock)
        try:
            task = asyncio.create_task(
                service.confirm(principal, order_id, quote.confirmation_id, True)
            )
            await asyncio.wait_for(waiting.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_product_lock)
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == stock
        assert (await session.get(Order, order_id)).status == "pending_payment"
    await service.confirm(principal, order_id, quote.confirmation_id, True)


async def test_postgres_lock_timeout_returns_retryable_http_503_and_keeps_transaction_clean(
    cancel_pg,
):
    service, principal, database, order_id, _, product_id, stock, _ = cancel_pg
    quote = await service.preview(principal, order_id, "锁等待超时重试")
    app = create_app()
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[get_database] = lambda: database
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        async with database.session() as holder:
            await holder.scalar(select(Product).where(Product.id == product_id).with_for_update())
            response = await client.post(
                f"/api/orders/{order_id}/cancel",
                json={"confirmation_id": str(quote.confirmation_id), "confirmed": True},
            )
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "DATABASE_BUSY"
            assert response.headers["Retry-After"] == "2"
        async with database.session() as session:
            assert (await session.get(Product, product_id)).stock == stock
            assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 0
        retry = await client.post(
            f"/api/orders/{order_id}/cancel",
            json={"confirmation_id": str(quote.confirmation_id), "confirmed": True},
        )
        assert retry.status_code == 200, retry.text


async def test_postgres_order_detail_uses_one_snapshot_during_cancellation(cancel_pg):
    service, principal, database, order_id, _, _, _, _ = cancel_pg
    quote = await service.preview(principal, order_id, "读取中并发取消")
    reader_database = Database(database.engine)
    original_session = reader_database.session
    interleaved = False

    @asynccontextmanager
    async def reading_session():
        nonlocal interleaved
        async with original_session() as session:
            original_execute = session.execute

            async def execute(statement, *args, **kwargs):
                nonlocal interleaved
                result = await original_execute(statement, *args, **kwargs)
                columns = getattr(statement, "column_descriptions", [])
                if not interleaved and columns and columns[0].get("entity") is Order:
                    interleaved = True
                    await service.confirm(principal, order_id, quote.confirmation_id, True)
                return result

            session.execute = execute
            yield session

    reader_database.session = reading_session
    detail = await OrderStore(reader_database).detail(order_id, principal.account.id)
    assert interleaved
    assert detail.status == "pending_payment" and detail.cancellation is None
    fresh = await OrderStore(database).detail(order_id, principal.account.id)
    assert fresh.status == "cancelled" and fresh.cancellation is not None

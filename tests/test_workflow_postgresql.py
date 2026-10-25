# ruff: noqa: F811
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_catalog_postgresql import catalog_pg_auth  # noqa: F401
from test_manual_catalog import manual_payload
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.cancellation_store import CancellationStore
from backend.db.catalog_store import CatalogStore
from backend.db.checkout_store import CheckoutStore
from backend.db.workflow_store import WorkflowStore
from backend.models.catalog import ManualProduct
from backend.models.checkout import CheckoutRequest
from backend.models.commerce import InventoryReservation, Order, Product, Shipment
from backend.models.fulfilment import OrderEvent, Payment, Refund, ReturnCase
from backend.models.workflow import Confirmation, ReturnCommand, ReviewCommand, ShipmentCommand
from backend.services.cancellations import CancellationService
from backend.services.workflow import WorkflowService

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires PostgreSQL"
)


@pytest_asyncio.fixture
async def workflow_pg(catalog_pg_auth):
    auth = catalog_pg_auth
    principals = [
        await auth.authenticate((await auth.login(name, "Postgres-test-password")).access_token)
        for name in ("admin", "alice", "bob")
    ]
    admin, user, bob = principals
    database = auth.store.database
    product = (
        await CatalogStore(database).create_manual(admin, ManualProduct(**manual_payload(stock=6)))
    ).product
    request = CheckoutRequest(
        request_id=uuid4(),
        product_id=product.id,
        quantity=2,
        expected_price_cents=product.price_cents,
    )
    order = (await CheckoutStore(database).create(user, request)).order
    return (
        WorkflowService(WorkflowStore(database)),
        CancellationService(CancellationStore(database)),
        database,
        admin,
        user,
        bob,
        order,
        product,
        auth,
    )


def confirm():
    return Confirmation(operation_id=uuid4(), confirmed=True)


async def pg_deliver(bundle):
    workflow, _, _, admin, user, _, order, _, _ = bundle
    await workflow.run(user, order.id, "pay", confirm())
    await workflow.run(
        admin,
        order.id,
        "ship",
        ShipmentCommand(
            operation_id=uuid4(), confirmed=True, carrier="演示快递", tracking_number="SF-FWD-001"
        ),
    )
    await workflow.run(user, order.id, "receive", confirm())


async def pg_returning(bundle):
    workflow, _, _, admin, user, _, order, _, _ = bundle
    await pg_deliver(bundle)
    await workflow.run(
        user,
        order.id,
        "return_request",
        ReturnCommand(operation_id=uuid4(), confirmed=True, reason="申请整单退货"),
    )
    await workflow.run(
        admin,
        order.id,
        "return_review",
        ReviewCommand(
            operation_id=uuid4(), confirmed=True, reason="核对通过，同意退货", decision="approve"
        ),
    )
    await workflow.run(
        user,
        order.id,
        "return_ship",
        ShipmentCommand(
            operation_id=uuid4(), confirmed=True, carrier="退货快递", tracking_number="SF-RET-001"
        ),
    )


async def test_payment_cancel_race_cannot_leave_paid_order_with_released_stock(workflow_pg):
    workflow, cancel, database, _, user, _, order, product, _ = workflow_pg
    quote = await cancel.preview(user, order.id, "取消付款前订单")
    results = await asyncio.gather(
        workflow.run(user, order.id, "pay", confirm()),
        cancel.confirm(user, order.id, quote.confirmation_id, True),
        return_exceptions=True,
    )
    assert sum(isinstance(row, Exception) for row in results) == 1
    async with database.session() as session:
        state = (await session.get(Order, order.id)).status
        stock = (await session.get(Product, product.id)).stock
        reservation = await session.scalar(select(InventoryReservation))
        if state == "paid":
            assert stock == 4 and reservation.released_at is None
            assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        else:
            assert state == "cancelled" and stock == 6 and reservation.released_at is not None
            assert await session.scalar(select(func.count()).select_from(Payment)) == 0


async def test_shipping_cancel_race_only_one_transition_commits(workflow_pg):
    workflow, cancel, database, admin, user, _, order, product, _ = workflow_pg
    await workflow.run(user, order.id, "pay", confirm())
    quote = await cancel.preview(user, order.id, "取消发货前订单")
    results = await asyncio.gather(
        workflow.run(
            admin,
            order.id,
            "ship",
            ShipmentCommand(
                operation_id=uuid4(),
                confirmed=True,
                carrier="演示快递",
                tracking_number="SF-RACE-001",
            ),
        ),
        cancel.confirm(user, order.id, quote.confirmation_id, True),
        return_exceptions=True,
    )
    assert sum(isinstance(row, Exception) for row in results) == 1
    async with database.session() as session:
        state = (await session.get(Order, order.id)).status
        stock = (await session.get(Product, product.id)).stock
        reservation = await session.scalar(select(InventoryReservation))
        if state == "shipped":
            assert (
                stock == 4
                and reservation.consumed_at is not None
                and reservation.released_at is None
            )
            assert await session.scalar(select(func.count()).select_from(Shipment)) == 1
            assert await session.scalar(select(func.count()).select_from(Refund)) == 0
        else:
            assert (
                state == "cancelled"
                and stock == 6
                and reservation.released_at is not None
                and reservation.consumed_at is None
            )
            assert await session.scalar(select(func.count()).select_from(Shipment)) == 0
            assert await session.scalar(select(func.count()).select_from(Refund)) == 1


async def test_parallel_inspection_and_refund_replay_restock_and_refund_once(workflow_pg):
    workflow, _, database, admin, _, _, order, product, _ = workflow_pg
    await pg_returning(workflow_pg)
    for action in ("return_receive", "return_refund"):
        body = confirm()
        results = await asyncio.gather(
            *(workflow.run(admin, order.id, action, body) for _ in range(12))
        )
        assert all(row == results[0] for row in results)
    async with database.session() as session:
        assert (await session.get(Product, product.id)).stock == 6
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        assert (await session.scalar(select(ReturnCase))).status == "closed"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrderEvent)
                .where(OrderEvent.action == "return_receive")
            )
            == 1
        )


async def test_inspection_audit_failure_rolls_back_restock_and_case_state(workflow_pg):
    workflow, _, database, admin, _, _, order, product, _ = workflow_pg
    await pg_returning(workflow_pg)

    def fail(session, *_):
        if any(
            isinstance(row, OrderEvent) and row.action == "return_receive" for row in session.new
        ):
            raise RuntimeError("audit failed")

    event.listen(Session, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="audit failed"):
            await workflow.run(admin, order.id, "return_receive", confirm())
    finally:
        event.remove(Session, "before_flush", fail)
    async with database.session() as session:
        assert (await session.get(Product, product.id)).stock == 4
        case = await session.scalar(select(ReturnCase))
        assert case.status == "returning" and case.restocked_at is None

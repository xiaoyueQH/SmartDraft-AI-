from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select
from test_auth import auth_api, headers, login  # noqa: F401

from backend.db.cancellation_store import CancellationStore
from backend.models.auth import AuthError
from backend.models.commerce import (
    CancellationConfirmation,
    InventoryReservation,
    Order,
    OrderCancellation,
    OrderItem,
    Product,
    Shipment,
)
from backend.models.fulfilment import Payment
from backend.services.cancellations import CancellationService


@pytest_asyncio.fixture
async def cancel_api(auth_api):  # noqa: F811
    client, auth, database = auth_api
    alice, bob = await auth.store.find_user("alice"), await auth.store.find_user("bob")
    now = datetime.now(UTC)
    product_id = uuid4()
    ids, item_ids = {}, {}
    async with database.session() as session:
        session.add(
            Product(
                id=product_id,
                sku="CANCEL-TEST",
                name="测试商品",
                description="演示",
                price_cents=1000,
                stock=20,
                is_demo=True,
            )
        )
        await session.flush()
        for key, owner, status, is_demo, quantity in (
            ("pending", alice.id, "pending_payment", True, 1),
            ("paid", alice.id, "paid", True, 2),
            ("shipped", alice.id, "shipped", True, 1),
            ("real", alice.id, "paid", False, 1),
            ("bob", bob.id, "pending_payment", True, 1),
        ):
            ids[key], item_ids[key] = uuid4(), uuid4()
            session.add(
                Order(
                    id=ids[key],
                    number=f"CANCEL-{key}",
                    user_id=owner,
                    status=status,
                    total_cents=1000 * quantity,
                    created_at=now,
                    is_demo=is_demo,
                )
            )
            await session.flush()
            if status in {"paid", "shipped"} and is_demo:
                session.add(
                    Payment(
                        order_id=ids[key], amount_cents=1000 * quantity, channel="demo", paid_at=now
                    )
                )
            session.add(
                OrderItem(
                    id=item_ids[key],
                    order_id=ids[key],
                    product_id=product_id,
                    sku="CANCEL-TEST",
                    product_name="测试商品",
                    unit_price_cents=1000,
                    quantity=quantity,
                )
            )
            await session.flush()
            session.add(
                InventoryReservation(order_item_id=item_ids[key], quantity=quantity, created_at=now)
            )
    identity = headers(await login(client))
    yield client, identity, ids, item_ids, product_id, database, auth


async def preview(client, identity, order_id, reason="不再需要"):
    return await client.post(
        f"/api/orders/{order_id}/cancellation-preview", headers=identity, json={"reason": reason}
    )


async def commit(client, identity, order_id, confirmation_id, **extra):
    return await client.post(
        f"/api/orders/{order_id}/cancel",
        headers=identity,
        json={"confirmation_id": str(confirmation_id), "confirmed": True, **extra},
    )


async def test_preview_confirmation_audit_and_repeat_commit_are_one_atomic_result(cancel_api):
    client, identity, ids, items, product_id, database, _ = cancel_api
    response = await preview(client, identity, ids["pending"], "  不再需要  ")
    assert response.status_code == 200, response.text
    quote = response.json()
    assert quote["reason"] == "不再需要" and quote["refund_status"] == "not_required"
    assert (await preview(client, identity, ids["pending"])).json()["confirmation_id"] == quote[
        "confirmation_id"
    ]
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == 20
        assert (await session.get(Order, ids["pending"])).status == "pending_payment"
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 0
    result = await commit(client, identity, ids["pending"], quote["confirmation_id"])
    assert result.status_code == 200, result.text
    assert result.json()["released_units"] == 1
    again = await commit(client, identity, ids["pending"], quote["confirmation_id"])
    assert again.json() == result.json()
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == 21
        assert (await session.get(InventoryReservation, items["pending"])).released_at is not None
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 1
    detail = (await client.get(f"/api/orders/{ids['pending']}", headers=identity)).json()
    assert detail["status"] == "cancelled"
    assert detail["cancellation"]["actor_username"] == "alice"
    assert detail["cancellation"]["reason"] == "不再需要"


async def test_paid_demo_records_simulation_while_real_payment_cannot_be_cancelled(cancel_api):
    client, identity, ids, _, _, _, _ = cancel_api
    quote = (await preview(client, identity, ids["paid"])).json()
    assert quote["refund_status"] == "simulated" and quote["refund_cents"] == 2000
    receipt = (await commit(client, identity, ids["paid"], quote["confirmation_id"])).json()
    assert receipt["is_demo"] is True and receipt["refund_status"] == "simulated"
    blocked = await preview(client, identity, ids["real"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "REFUND_UNAVAILABLE"


@pytest.mark.parametrize("status", ["shipped", "delivered", "cancelled"])
async def test_closed_or_shipped_orders_are_rejected(cancel_api, status):
    client, identity, ids, _, product_id, database, _ = cancel_api
    async with database.session() as session:
        (await session.get(Order, ids["pending"])).status = status
    assert (await preview(client, identity, ids["pending"])).status_code == 409
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == 20


async def test_confirmation_requires_explicit_true_and_reason(cancel_api):
    client, identity, ids, _, _, _, _ = cancel_api
    quote = (await preview(client, identity, ids["pending"])).json()
    assert (
        await commit(client, identity, ids["pending"], quote["confirmation_id"], confirmed=False)
    ).status_code == 422
    assert (
        await client.post(
            f"/api/orders/{ids['pending']}/cancel",
            headers=identity,
            json={"confirmation_id": quote["confirmation_id"]},
        )
    ).status_code == 422
    assert (await preview(client, identity, ids["pending"], "  ")).status_code == 422
    assert (await commit(client, identity, ids["pending"], uuid4())).status_code == 409


@pytest.mark.parametrize("confirmed", [1, "true", "1", None])
async def test_confirmation_does_not_coerce_non_boolean_values(cancel_api, confirmed):
    client, identity, ids, _, _, _, _ = cancel_api
    quote = (await preview(client, identity, ids["pending"])).json()
    assert (
        await commit(
            client, identity, ids["pending"], quote["confirmation_id"], confirmed=confirmed
        )
    ).status_code == 422


async def test_order_and_confirmation_are_bound_to_owner_and_login_session(cancel_api):
    client, identity, ids, _, _, _, _ = cancel_api
    assert (await preview(client, identity, ids["bob"])).status_code == 404
    quote = (await preview(client, identity, ids["pending"])).json()
    second_login = headers(await login(client))
    assert (
        await commit(client, second_login, ids["pending"], quote["confirmation_id"])
    ).status_code == 409
    assert (
        await commit(client, identity, ids["paid"], quote["confirmation_id"])
    ).status_code == 409
    assert (
        await commit(client, identity, ids["pending"], quote["confirmation_id"])
    ).status_code == 200


async def test_admin_can_cancel_other_owners_but_normal_user_cannot_use_admin_routes(cancel_api):
    client, identity, ids, _, _, _, _ = cancel_api
    path = f"/api/admin/orders/{ids['bob']}"
    assert (
        await client.post(path + "/cancellation-preview", headers=identity, json={"reason": "测试"})
    ).status_code == 403
    admin = headers(await login(client, "admin"))
    quote = (
        await client.post(
            path + "/cancellation-preview", headers=admin, json={"reason": "用户联系取消"}
        )
    ).json()
    result = await client.post(
        path + "/cancel",
        headers=admin,
        json={"confirmation_id": quote["confirmation_id"], "confirmed": True},
    )
    assert result.status_code == 200
    assert result.json()["actor_role"] == "admin" and result.json()["actor_username"] == "admin"


@pytest.mark.parametrize("change", ["expired", "snapshot", "shipment", "reservation"])
async def test_execution_rechecks_expiry_snapshot_logistics_and_stock_reservation(
    cancel_api, change
):
    client, identity, ids, items, product_id, database, _ = cancel_api
    quote = (await preview(client, identity, ids["pending"])).json()
    async with database.session() as session:
        if change == "expired":
            (
                await session.get(CancellationConfirmation, UUID(quote["confirmation_id"]))
            ).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "snapshot":
            (await session.get(OrderItem, items["pending"])).product_name = "购买信息变化"
        elif change == "shipment":
            session.add(
                Shipment(
                    order_id=ids["pending"], carrier="测试", tracking_number="TEST", is_demo=True
                )
            )
        else:
            await session.execute(
                delete(InventoryReservation).where(
                    InventoryReservation.order_item_id == items["pending"]
                )
            )
    result = await commit(client, identity, ids["pending"], quote["confirmation_id"])
    assert result.status_code == 409
    assert (
        result.json()["error"]["code"]
        == {
            "expired": "CONFIRMATION_EXPIRED",
            "snapshot": "ORDER_CHANGED",
            "shipment": "SHIPMENT_EXISTS",
            "reservation": "RESERVATION_MISSING",
        }[change]
    )
    async with database.session() as session:
        assert (await session.get(Order, ids["pending"])).status == "pending_payment"
        assert (await session.get(Product, product_id)).stock == 20
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 0


async def test_database_failure_rolls_back_inventory_order_and_audit(cancel_api):
    client, identity, ids, items, product_id, database, _ = cancel_api
    quote = (await preview(client, identity, ids["pending"])).json()

    def fail_stock_update(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE products"):
            raise RuntimeError("injected stock write failure")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail_stock_update)
    try:
        assert (
            await commit(client, identity, ids["pending"], quote["confirmation_id"])
        ).status_code == 500
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", fail_stock_update)
    async with database.session() as session:
        assert (await session.get(Product, product_id)).stock == 20
        assert (await session.get(Order, ids["pending"])).status == "pending_payment"
        assert (await session.get(InventoryReservation, items["pending"])).released_at is None
        assert await session.scalar(select(func.count()).select_from(OrderCancellation)) == 0
    assert (
        await commit(client, identity, ids["pending"], quote["confirmation_id"])
    ).status_code == 200


async def test_service_rechecks_revoked_session_even_with_previous_principal(cancel_api):
    client, identity, ids, _, _, database, auth = cancel_api
    token = identity["Authorization"].removeprefix("Bearer ")
    principal = await auth.authenticate(token)
    quote = (await preview(client, identity, ids["pending"])).json()
    await auth.store.revoke(principal.account.id, principal.session_id)
    with pytest.raises(AuthError):
        await CancellationService(CancellationStore(database)).confirm(
            principal,
            ids["pending"],
            UUID(quote["confirmation_id"]),
            True,
        )

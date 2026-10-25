from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from test_auth import auth_api, headers, login  # noqa: F401

from backend.models.auth import AuthError, Principal
from backend.models.commerce import Order, OrderItem, Product, Shipment, ShipmentEvent
from backend.services.orders import OrderService


@pytest_asyncio.fixture
async def orders_api(auth_api):  # noqa: F811
    client, service, database = auth_api
    alice = await service.store.find_user("alice")
    bob = await service.store.find_user("bob")
    product_id, alice_id, second_id, bob_id, shipment_id = [uuid4() for _ in range(5)]
    now = datetime.now(UTC)
    async with database.session() as session:
        session.add(
            Product(
                id=product_id,
                sku="NEW-SKU",
                name="当前商品名称",
                description="商品说明",
                price_cents=99999,
                stock=8,
                is_demo=True,
            )
        )
        await session.flush()
        for order_id, owner, number, status, created in (
            (alice_id, alice.id, "ALICE-1", "shipped", now),
            (second_id, alice.id, "ALICE-2", "paid", now - timedelta(days=1)),
            (bob_id, bob.id, "BOB-1", "paid", now),
        ):
            session.add(
                Order(
                    id=order_id,
                    number=number,
                    user_id=owner,
                    status=status,
                    total_cents=24600,
                    created_at=created,
                    is_demo=True,
                )
            )
        await session.flush()
        session.add(
            OrderItem(
                order_id=alice_id,
                product_id=product_id,
                sku="OLD-SKU",
                product_name="购买时的商品名称",
                unit_price_cents=12300,
                quantity=2,
            )
        )
        session.add(
            Shipment(
                id=shipment_id,
                order_id=alice_id,
                carrier="测试物流",
                tracking_number="DEMO-001",
                is_demo=True,
            )
        )
        await session.flush()
        for step, description in ((2, "运输中"), (1, "已出库")):
            session.add(
                ShipmentEvent(
                    shipment_id=shipment_id,
                    occurred_at=now + timedelta(hours=step),
                    description=description,
                )
            )
    yield client, {"alice": alice_id, "second": second_id, "bob": bob_id}, service


async def test_order_endpoints_require_login(orders_api):
    client, ids, _ = orders_api
    for path in (
        "/api/products",
        "/api/orders",
        f"/api/orders/{ids['alice']}",
        "/api/admin/orders",
        f"/api/admin/orders/{ids['alice']}",
    ):
        assert (await client.get(path)).status_code == 401


async def test_owner_scope_cannot_be_overridden_and_missing_orders_are_indistinguishable(
    orders_api,
):
    client, ids, _ = orders_api
    identity = headers(await login(client))
    page = (await client.get("/api/orders?all_users=true&username=bob", headers=identity)).json()
    assert {row["number"] for row in page["items"]} == {"ALICE-1", "ALICE-2"}
    foreign = await client.get(f"/api/orders/{ids['bob']}", headers=identity)
    missing = await client.get(f"/api/orders/{uuid4()}", headers=identity)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"]
    assert foreign.headers["Cache-Control"] == "no-store"
    assert (await client.get("/api/admin/orders", headers=identity)).status_code == 403
    assert (
        await client.get(f"/api/admin/orders/{ids['bob']}", headers=identity)
    ).status_code == 403


async def test_admin_can_read_all_orders_but_my_orders_remains_owner_scoped(orders_api):
    client, ids, _ = orders_api
    identity = headers(await login(client, "admin"))
    assert (await client.get("/api/orders", headers=identity)).json()["items"] == []
    page = (await client.get("/api/admin/orders", headers=identity)).json()
    assert {item["number"] for item in page["items"]} == {"ALICE-1", "ALICE-2", "BOB-1"}
    assert (
        await client.get(f"/api/admin/orders/{ids['bob']}", headers=identity)
    ).status_code == 200


async def test_order_snapshot_money_and_ordered_logistics(orders_api):
    client, ids, _ = orders_api
    identity = headers(await login(client))
    result = await client.get(f"/api/orders/{ids['alice']}", headers=identity)
    assert result.status_code == 200
    detail = result.json()
    assert detail["items"][0]["product_name"] == "购买时的商品名称"
    assert detail["items"][0]["sku"] == "OLD-SKU"
    assert detail["total_cents"] == sum(
        item["unit_price_cents"] * item["quantity"] for item in detail["items"]
    )
    assert [event["description"] for event in detail["shipments"][0]["events"]] == [
        "已出库",
        "运输中",
    ]
    assert detail["shipments"][0]["is_demo"] is True
    assert "password" not in result.text and "refresh" not in result.text
    product = (await client.get("/api/products", headers=identity)).json()["items"][0]
    assert product["price_cents"] == 99999
    assert product["name"] == "当前商品名称"


async def test_pagination_filter_and_empty_order_details(orders_api):
    client, ids, _ = orders_api
    identity = headers(await login(client))
    first = (await client.get("/api/orders?limit=1", headers=identity)).json()
    second = (await client.get("/api/orders?limit=1&offset=1", headers=identity)).json()
    assert first["has_more"] is True and second["has_more"] is False
    assert first["items"][0]["number"] == "ALICE-1"
    assert second["items"][0]["number"] == "ALICE-2"
    filtered = (await client.get("/api/orders?status=paid", headers=identity)).json()
    assert [item["number"] for item in filtered["items"]] == ["ALICE-2"]
    exact = (await client.get("/api/orders?number=ALICE-1", headers=identity)).json()
    assert len(exact["items"]) == 1
    empty = (await client.get(f"/api/orders/{ids['second']}", headers=identity)).json()
    assert empty["items"] == [] and empty["shipments"] == []


@pytest.mark.parametrize(
    "query", ["limit=101", "limit=0", "offset=-1", "offset=10001", "status=bad"]
)
async def test_order_query_budgets_are_validated(orders_api, query):
    client, _, _ = orders_api
    identity = headers(await login(client))
    assert (await client.get(f"/api/orders?{query}", headers=identity)).status_code == 422


async def test_service_rechecks_admin_scope_for_future_tool_callers(orders_api):
    _, _, auth = orders_api
    alice = await auth.store.find_user("alice")
    with pytest.raises(AuthError, match="需要管理员权限"):
        OrderService.scope(Principal(alice, uuid4()), True)

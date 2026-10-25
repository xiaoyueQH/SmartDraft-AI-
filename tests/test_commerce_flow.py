# ruff: noqa: F811
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_catalog import payload

from backend.models.commerce import Order, OrderItem, Product, ProductChange


@pytest_asyncio.fixture
async def commerce(auth_api):
    client, auth, database = auth_api
    admin = headers(await login(client, "admin"))
    user = headers(await login(client))
    response = await client.post("/api/admin/catalog/imports", headers=admin, json=payload(stock=8))
    assert response.status_code == 200, response.text
    return client, admin, user, database, response.json()["product"]


def mutation(product, **extra):
    return {"operation_id": str(uuid4()), "expected_version": product["version"], **extra}


def checkout(product, **extra):
    return {
        "request_id": str(uuid4()),
        "product_id": product["id"],
        "quantity": 2,
        "expected_price_cents": product["price_cents"],
        **extra,
    }


async def test_complete_import_edit_checkout_cancel_recycle_restore_flow(commerce):
    client, admin, user, database, product = commerce
    path = f"/api/admin/catalog/products/{product['id']}"
    edit = mutation(
        product, name="更新后的显卡", description="商品参数", price_cents=88888, stock=10
    )
    response = await client.patch(path, headers=admin, json=edit)
    assert response.status_code == 200, response.text
    product = response.json()
    assert product["version"] == 2
    assert (await client.patch(path, headers=admin, json=edit)).json() == product
    stale = await client.patch(path, headers=admin, json={**edit, "operation_id": str(uuid4())})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "PRODUCT_CHANGED"
    off = (
        await client.post(path + "/state", headers=admin, json=mutation(product, is_active=False))
    ).json()
    assert not off["is_active"]
    assert (await client.get("/api/products", headers=user)).json()["items"] == []
    assert (
        await client.post("/api/orders", headers=user, json=checkout(product))
    ).status_code == 409
    product = (
        await client.post(path + "/state", headers=admin, json=mutation(off, is_active=True))
    ).json()
    request = checkout(product)
    response = await client.post("/api/orders", headers=user, json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] and result["order"]["status"] == "pending_payment"
    order = result["order"]
    assert order["total_cents"] == 177776
    assert order["items"][0]["product_name"] == "更新后的显卡"
    replay = (await client.post("/api/orders", headers=user, json=request)).json()
    assert replay["created"] is False and replay["order"]["id"] == order["id"]
    product = (await client.get("/api/admin/catalog/products", headers=admin)).json()["items"][0]
    assert product["stock"] == 8
    deleted_request = mutation(product)
    deleted = (await client.request("DELETE", path, headers=admin, json=deleted_request)).json()
    assert deleted["deleted_at"] and not deleted["is_active"]
    assert (await client.get("/api/products", headers=user)).json()["items"] == []
    assert (await client.get("/api/admin/catalog/products", headers=admin)).json()["items"] == []
    assert (
        await client.request("DELETE", path, headers=admin, json=deleted_request)
    ).json() == deleted
    assert (
        await client.post("/api/admin/catalog/imports", headers=admin, json=payload())
    ).status_code == 409
    assert (await client.get(f"/api/orders/{order['id']}", headers=user)).status_code == 200
    preview = (
        await client.post(
            f"/api/orders/{order['id']}/cancellation-preview",
            headers=user,
            json={"reason": "不需要了"},
        )
    ).json()
    cancelled = await client.post(
        f"/api/orders/{order['id']}/cancel",
        headers=user,
        json={"confirmation_id": preview["confirmation_id"], "confirmed": True},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["released_units"] == 2
    deleted = (await client.get("/api/admin/catalog/products?deleted=true", headers=admin)).json()[
        "items"
    ][0]
    assert deleted["stock"] == 10
    restored = (await client.post(path + "/restore", headers=admin, json=mutation(deleted))).json()
    assert restored["deleted_at"] is None and not restored["is_active"]
    assert (await client.get("/api/products", headers=user)).json()["items"] == []
    await client.post(path + "/state", headers=admin, json=mutation(restored, is_active=True))
    assert len((await client.get("/api/products", headers=user)).json()["items"]) == 1
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(ProductChange)) == 6


async def test_checkout_payload_ownership_and_request_identity(commerce):
    client, admin, user, _, product = commerce
    request = checkout(product)
    created = (await client.post("/api/orders", headers=user, json=request)).json()["order"]
    reused = await client.post("/api/orders", headers=user, json={**request, "quantity": 1})
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "ORDER_REQUEST_REUSED"
    bob = headers(await login(client, "bob"))
    assert (await client.get(f"/api/orders/{created['id']}", headers=bob)).status_code == 404
    # 同一个请求 ID 在另一账号下独立创建，不复用或泄露上个账号的订单。
    other = (await client.post("/api/orders", headers=bob, json=request)).json()
    assert other["created"] and other["order"]["id"] != created["id"]
    for override in (
        {"quantity": 0},
        {"quantity": True},
        {"quantity": 100},
        {"user_id": str(uuid4())},
        {"expected_price_cents": 1.2},
    ):
        assert (
            await client.post("/api/orders", headers=user, json={**checkout(product), **override})
        ).status_code == 422
    for override, code in (
        ({"quantity": 99}, "INSUFFICIENT_STOCK"),
        ({"expected_price_cents": 1}, "PRICE_CHANGED"),
    ):
        response = await client.post(
            "/api/orders", headers=user, json={**checkout(product), **override}
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == code


async def test_catalog_changes_require_admin_and_cannot_mutate_deleted_product(commerce):
    client, admin, user, _, product = commerce
    path = f"/api/admin/catalog/products/{product['id']}"
    for method, suffix, body in (
        (
            "PATCH",
            "",
            {**mutation(product), **{k: v for k, v in payload().items() if k != "candidate_id"}},
        ),
        ("POST", "/state", mutation(product, is_active=False)),
        ("DELETE", "", mutation(product)),
        ("POST", "/restore", mutation(product)),
    ):
        for identity, status in (({}, 401), (user, 403)):
            assert (
                await client.request(method, path + suffix, json=body, headers=identity)
            ).status_code == status
    body = mutation(product, is_active=False)
    changed = await client.post(path + "/state", headers=admin, json=body)
    assert changed.status_code == 200
    response = await client.post(path + "/state", headers=admin, json={**body, "is_active": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "OPERATION_REUSED"
    product = changed.json()
    deleted = (await client.request("DELETE", path, json=mutation(product), headers=admin)).json()
    assert (
        await client.post(path + "/state", json=mutation(deleted, is_active=True), headers=admin)
    ).status_code == 409


async def test_admin_stock_edit_cannot_overwrite_stock_changed_by_checkout(commerce):
    client, admin, user, database, product = commerce
    await client.post("/api/orders", json=checkout(product), headers=user)
    response = await client.patch(
        f"/api/admin/catalog/products/{product['id']}",
        headers=admin,
        json=mutation(product, name="过期编辑", description="test", price_cents=100, stock=100),
    )
    assert response.status_code == 409
    async with database.session() as session:
        persisted = await session.get(Product, UUID(product["id"]))
        assert persisted.stock == 6
        assert (await session.scalar(select(OrderItem))).unit_price_cents == 123456

# ruff: noqa: F811
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401

from backend.models.commerce import CatalogImport, Product


def manual_payload(**extra):
    return {
        "request_id": str(uuid4()),
        "sku": "own-monitor-001",
        "name": "我的办公显示器",
        "description": "27英寸，2K分辨率，三年保修",
        "price_cents": 129999,
        "stock": 20,
        **extra,
    }


async def test_manual_entry_requires_admin_and_saves_own_sku_with_provenance(auth_api):
    client, _, database = auth_api
    payload = manual_payload()
    assert (await client.post("/api/admin/catalog/products", json=payload)).status_code == 401
    assert (
        await client.post(
            "/api/admin/catalog/products", json=payload, headers=headers(await login(client))
        )
    ).status_code == 403
    identity = headers(await login(client, "admin"))
    response = await client.post("/api/admin/catalog/products", json=payload, headers=identity)
    assert response.status_code == 200, response.text
    product = response.json()["product"]
    assert product["sku"] == "OWN-MONITOR-001" and product["name"] == payload["name"]
    assert product["is_demo"] is False and product["stock"] == 20
    repeated = await client.post("/api/admin/catalog/products", json=payload, headers=identity)
    assert repeated.json() == {"created": False, "product": product}
    changed = await client.post(
        "/api/admin/catalog/products", json={**payload, "stock": 40}, headers=identity
    )
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "REQUEST_REUSED"
    duplicate = await client.post(
        "/api/admin/catalog/products",
        json={**payload, "request_id": str(uuid4())},
        headers=identity,
    )
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "SKU_EXISTS"
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        audit = await session.scalar(select(CatalogImport))
        assert (
            audit.source == "manual"
            and audit.candidate_snapshot["input"]["sku"] == "OWN-MONITOR-001"
        )


async def test_own_product_can_be_ordered_cancelled_and_preserves_source(auth_api):
    client, _, database = auth_api
    admin = headers(await login(client, "admin"))
    product = (
        await client.post("/api/admin/catalog/products", headers=admin, json=manual_payload())
    ).json()["product"]
    user = headers(await login(client))
    response = await client.post(
        "/api/orders",
        headers=user,
        json={
            "request_id": str(uuid4()),
            "product_id": product["id"],
            "quantity": 2,
            "expected_price_cents": product["price_cents"],
        },
    )
    assert response.status_code == 200, response.text
    order = response.json()["order"]
    assert order["is_demo"] is True  # 业务环境与商品来源分开，未发起真实支付。
    preview = (
        await client.post(
            f"/api/orders/{order['id']}/cancellation-preview",
            headers=user,
            json={"reason": "取消自有商品订单"},
        )
    ).json()
    receipt = await client.post(
        f"/api/orders/{order['id']}/cancel",
        headers=user,
        json={"confirmation_id": preview["confirmation_id"], "confirmed": True},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["released_units"] == 2
    async with database.session() as session:
        row = await session.get(Product, UUID(product["id"]))
        assert row.stock == 20 and not row.is_demo


@pytest.mark.parametrize(
    "extra",
    [
        {"sku": "a"},
        {"sku": "自有商品"},
        {"sku": "space sku"},
        {"sku": "X" * 33},
        {"name": " "},
        {"price_cents": 1.2},
        {"stock": True},
        {"is_demo": True},
        {"source": "taobao"},
    ],
)
async def test_manual_fields_are_validated_and_source_cannot_be_spoofed(auth_api, extra):
    client, _, _ = auth_api
    assert (
        await client.post(
            "/api/admin/catalog/products",
            headers=headers(await login(client, "admin")),
            json=manual_payload(**extra),
        )
    ).status_code == 422

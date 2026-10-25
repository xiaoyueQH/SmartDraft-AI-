# ruff: noqa: F811
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_manual_catalog import manual_payload

from backend.models.commerce import InventoryReservation, Product
from backend.models.fulfilment import Payment, Refund, ReturnCase

PATHS = {
    "pay": "/orders/{}/pay",
    "ship": "/admin/orders/{}/ship",
    "receive": "/orders/{}/receive",
    "return_request": "/orders/{}/returns",
    "return_review": "/admin/orders/{}/returns/review",
    "return_ship": "/orders/{}/returns/ship",
    "return_receive": "/admin/orders/{}/returns/receive",
    "return_refund": "/admin/orders/{}/returns/refund",
}
ADMIN = {"ship", "return_review", "return_receive", "return_refund"}


@pytest.mark.parametrize("mismatch", [False, True])
async def test_payment_evidence_is_required_before_shipping_or_paid_cancellation(
    workflow_api, mismatch
):
    client, admin, user, database, order, _, _ = workflow_api
    await advance(workflow_api, "pay")
    async with database.session() as session:
        if mismatch:
            (await session.scalar(select(Payment))).amount_cents += 1
        else:
            await session.execute(delete(Payment))
    shipping = await client.post(
        "/api" + PATHS["ship"].format(order["id"]),
        headers=admin,
        json=command(carrier="演示快递", tracking_number="SF-INVALID-001"),
    )
    cancel = await client.post(
        f"/api/orders/{order['id']}/cancellation-preview", headers=user, json={"reason": "申请取消"}
    )
    for response in (shipping, cancel):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PAYMENT_INVALID"
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0


def command(**extra):
    return {"operation_id": str(uuid4()), "confirmed": True, **extra}


@pytest_asyncio.fixture
async def workflow_api(auth_api):
    client, auth, database = auth_api
    admin = headers(await login(client, "admin"))
    user = headers(await login(client))
    product = (
        await client.post(
            "/api/admin/catalog/products", headers=admin, json=manual_payload(stock=5)
        )
    ).json()["product"]
    order = (
        await client.post(
            "/api/orders",
            headers=user,
            json={
                "request_id": str(uuid4()),
                "product_id": product["id"],
                "quantity": 2,
                "expected_price_cents": product["price_cents"],
            },
        )
    ).json()["order"]
    return client, admin, user, database, order, product, auth


async def advance(bundle, action, **extra):
    client, admin, user, _, order, _, _ = bundle
    response = await client.post(
        "/api" + PATHS[action].format(order["id"]),
        headers=admin if action in ADMIN else user,
        json=command(**extra),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def deliver(bundle):
    await advance(bundle, "pay")
    await advance(bundle, "ship", carrier="演示快递", tracking_number="SF-FORWARD-001")
    await advance(bundle, "receive")


async def returning(bundle):
    await deliver(bundle)
    await advance(bundle, "return_request", reason="尺寸不合适，需要退货")
    await advance(bundle, "return_review", reason="已核对，同意整单退货", decision="approve")
    await advance(bundle, "return_ship", carrier="退货快递", tracking_number="SF-RETURN-001")


async def test_complete_fulfilment_and_return_refund_is_persisted_and_idempotent(workflow_api):
    client, admin, user, database, order, product, _ = workflow_api
    await returning(workflow_api)
    for action in ("return_receive", "return_refund"):
        body = command()
        path = "/api" + PATHS[action].format(order["id"])
        first = await client.post(path, headers=admin, json=body)
        assert first.status_code == 200, first.text
        assert (await client.post(path, headers=admin, json=body)).json() == first.json()
        assert (await client.post(path, headers=admin, json=command())).status_code == 409
    detail = (await client.get(f"/api/orders/{order['id']}", headers=user)).json()
    assert detail["status"] == "delivered" and detail["after_sale"]["status"] == "closed"
    assert (
        detail["payment"]["amount_cents"]
        == detail["refund"]["amount_cents"]
        == order["total_cents"]
    )
    assert detail["payment"]["channel"] == detail["refund"]["channel"] == "demo"
    assert [row["action"] for row in detail["events"]] == [
        "created",
        "pay",
        "ship",
        "receive",
        "return_request",
        "return_review",
        "return_ship",
        "return_receive",
        "return_refund",
    ]
    for endpoint in ("/api/returns", "/api/admin/returns"):
        page = (
            await client.get(
                endpoint + "?status=closed&limit=1", headers=admin if "admin" in endpoint else user
            )
        ).json()
        assert page["items"][0]["order_id"] == order["id"] and not page["has_more"]
    async with database.session() as session:
        assert (await session.get(Product, UUID(product["id"]))).stock == 5
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        reservation = await session.scalar(select(InventoryReservation))
        assert reservation.consumed_at and reservation.released_at is None


async def test_roles_ownership_and_explicit_confirmation_are_enforced(workflow_api):
    client, admin, user, _, order, _, _ = workflow_api
    bob = headers(await login(client, "bob"))
    for action in PATHS:
        body = (
            command(carrier="演示快递", tracking_number="DEMO-001")
            if action in ("ship", "return_ship")
            else command(
                reason="退货申请", **({"decision": "approve"} if action == "return_review" else {})
            )
            if action in ("return_request", "return_review")
            else command()
        )
        path = "/api" + PATHS[action].format(order["id"])
        assert (await client.post(path, json=body)).status_code == 401
        assert (
            await client.post(path, headers=user if action in ADMIN else bob, json=body)
        ).status_code == (403 if action in ADMIN else 404)
    for confirmed in (False, 1, "true"):
        assert (
            await client.post(
                f"/api/orders/{order['id']}/pay", headers=user, json=command(confirmed=confirmed)
            )
        ).status_code == 422
    assert (await client.get("/api/admin/returns", headers=user)).status_code == 403
    assert (await client.get("/api/returns", headers=bob)).json()["items"] == []
    for query in ("limit=101", "offset=10001", "status=unknown"):
        assert (await client.get("/api/returns?" + query, headers=user)).status_code == 422


async def test_cannot_skip_states_or_ship_with_consumed_or_released_reservations(workflow_api):
    client, admin, user, database, order, _, _ = workflow_api
    assert (
        await client.post(
            "/api" + PATHS["ship"].format(order["id"]),
            headers=admin,
            json=command(carrier="演示快递", tracking_number="DEMO-001"),
        )
    ).status_code == 409
    assert (
        await client.post(
            "/api" + PATHS["return_request"].format(order["id"]),
            headers=user,
            json=command(reason="申请退货"),
        )
    ).status_code == 409
    await advance(workflow_api, "pay")
    async with database.session() as session:
        (await session.scalar(select(InventoryReservation))).consumed_at = datetime.now(UTC)
    response = await client.post(
        "/api" + PATHS["ship"].format(order["id"]),
        headers=admin,
        json=command(carrier="演示快递", tracking_number="DEMO-001"),
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "INVENTORY_INVALID"


async def test_rejected_case_is_terminal_and_does_not_change_inventory(workflow_api):
    client, admin, user, database, order, product, _ = workflow_api
    await deliver(workflow_api)
    await advance(workflow_api, "return_request", reason="申请整单退货")
    await advance(
        workflow_api, "return_review", decision="reject", reason="申请信息不完整，已记录驳回原因"
    )
    for action in ("return_ship", "return_receive", "return_refund"):
        extra = (
            {"carrier": "退货快递", "tracking_number": "RET-001"} if action == "return_ship" else {}
        )
        response = await client.post(
            "/api" + PATHS[action].format(order["id"]),
            headers=admin if action in ADMIN else user,
            json=command(**extra),
        )
        assert response.status_code == 409
    async with database.session() as session:
        assert (await session.get(Product, UUID(product["id"]))).stock == 3
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0
        assert (await session.scalar(select(ReturnCase))).status == "rejected"


async def test_paid_cancellation_records_one_refund_and_original_operation_cannot_be_reused(
    workflow_api,
):
    client, _, user, database, order, _, _ = workflow_api
    body = command()
    path = "/api" + PATHS["pay"].format(order["id"])
    first = await client.post(path, headers=user, json=body)
    assert (await client.post(path, headers=user, json=body)).json() == first.json()
    reused = await client.post(
        "/api" + PATHS["receive"].format(order["id"]), headers=user, json=body
    )
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "OPERATION_REUSED"
    preview = (
        await client.post(
            f"/api/orders/{order['id']}/cancellation-preview",
            headers=user,
            json={"reason": "取消已付款演示订单"},
        )
    ).json()
    cancel = {"confirmation_id": preview["confirmation_id"], "confirmed": True}
    for _ in range(2):
        assert (
            await client.post(f"/api/orders/{order['id']}/cancel", headers=user, json=cancel)
        ).status_code == 200
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1

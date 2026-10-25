# ruff: noqa: F811
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401

from backend.db.catalog_store import CatalogStore
from backend.models.auth import AuthError, Principal
from backend.models.catalog import ImportProduct
from backend.models.commerce import CatalogImport, Product
from backend.models.orm import LoginSession, User
from backend.services.catalog import CatalogService
from backend.services.mock_catalog import CATALOG


def payload(candidate_id="GPU-001", **overrides):
    return {
        "candidate_id": candidate_id,
        "name": "星澜显卡（模拟）",
        "description": "已核对的模拟显卡参数",
        "price_cents": 123456,
        "stock": 17,
        **overrides,
    }


async def test_catalog_requires_login_and_admin_for_all_operations(auth_api):
    client, _, _ = auth_api
    for identity, status in (({}, 401), (headers(await login(client)), 403)):
        for endpoint in ("candidates", "products"):
            assert (
                await client.get(f"/api/admin/catalog/{endpoint}", headers=identity)
            ).status_code == status
        assert (
            await client.post("/api/admin/catalog/imports", json=payload(), headers=identity)
        ).status_code == status


async def test_search_filters_paginates_and_never_seeds_products(auth_api):
    client, _, database = auth_api
    identity = headers(await login(client, "admin"))
    first = (
        await client.get("/api/admin/catalog/candidates?q=显卡&limit=2", headers=identity)
    ).json()
    second = (
        await client.get("/api/admin/catalog/candidates?q=显卡&limit=2&offset=2", headers=identity)
    ).json()
    assert len(first["items"]) == 2 and first["has_more"]
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert all(row["source"] == "mock_catalog_v1" and row["is_demo"] for row in first["items"])
    assert all(row["imported_product_id"] is None for row in first["items"])
    exact = (
        await client.get(
            "/api/admin/catalog/candidates?q=显卡 16GB&category=显卡", headers=identity
        )
    ).json()
    assert {row["id"] for row in exact["items"]} == {"GPU-004", "GPU-005"}
    assert (await client.get("/api/admin/catalog/candidates?q=不存在", headers=identity)).json()[
        "items"
    ] == []
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 0


async def test_import_persists_edited_values_audit_and_is_visible_to_users(auth_api):
    client, _, database = auth_api
    identity = headers(await login(client, "admin"))
    response = await client.post("/api/admin/catalog/imports", headers=identity, json=payload())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] is True
    product = result["product"]
    assert product["sku"] == "MOCK-GPU-001" and product["is_demo"] is True
    assert product["price_cents"] == 123456 and product["stock"] == 17
    assert product["description"] == payload()["description"]
    repeated = (
        await client.post(
            "/api/admin/catalog/imports",
            headers=identity,
            json=payload(price_cents=99, stock=900, name="不可覆盖"),
        )
    ).json()
    assert repeated == {"created": False, "product": product}
    candidate = (await client.get("/api/admin/catalog/candidates?q=G60", headers=identity)).json()[
        "items"
    ][0]
    assert candidate["imported_product_id"] == product["id"]
    admin_page = (await client.get("/api/admin/catalog/products", headers=identity)).json()
    assert admin_page["items"] == [product]
    customer_page = (await client.get("/api/products", headers=headers(await login(client)))).json()
    assert customer_page["items"] == [product]
    async with database.session() as session:
        audit = await session.scalar(select(CatalogImport))
        assert audit.source_id == "GPU-001"
        assert audit.actor_id is not None and audit.created_at is not None
        assert audit.candidate_snapshot["specifications"]["显存"] == "8GB"
        assert await session.scalar(select(func.count()).select_from(Product)) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_cents": -1},
        {"price_cents": 0},
        {"price_cents": 100000001},
        {"price_cents": 1.23},
        {"price_cents": True},
        {"stock": -1},
        {"stock": 1000001},
        {"stock": "10"},
        {"name": "  "},
        {"description": ""},
        {"source": "taobao"},
        {"is_demo": False},
    ],
)
async def test_import_validates_money_inventory_and_cannot_spoof_source(auth_api, overrides):
    client, _, _ = auth_api
    response = await client.post(
        "/api/admin/catalog/imports",
        json=payload(**overrides),
        headers=headers(await login(client, "admin")),
    )
    assert response.status_code == 422


async def test_unknown_candidate_and_bounded_queries(auth_api):
    client, _, _ = auth_api
    identity = headers(await login(client, "admin"))
    response = await client.post(
        "/api/admin/catalog/imports", json=payload("NOT-A-CANDIDATE"), headers=identity
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"
    for query in ("limit=101", "offset=10001", "limit=0", "q=" + "x" * 81):
        assert (
            await client.get(f"/api/admin/catalog/candidates?{query}", headers=identity)
        ).status_code == 422


@pytest.mark.parametrize("change", ["role", "revoked", "password", "disabled"])
async def test_import_rechecks_identity_inside_write_transaction(auth_api, change):
    client, auth, database = auth_api
    tokens = await login(client, "admin")
    principal = await auth.authenticate(tokens["access_token"])
    async with database.session() as session:
        user = await session.get(User, principal.account.id)
        if change == "role":
            user.role = "user"
        elif change == "password":
            user.password_hash = "changed"
        elif change == "disabled":
            user.is_active = False
        else:
            (await session.get(LoginSession, principal.session_id)).revoked_at = datetime.now(UTC)
    with pytest.raises(AuthError) as error:
        await CatalogService(CatalogStore(database)).import_product(
            principal, ImportProduct(**payload())
        )
    assert error.value.status == (403 if change == "role" else 401)
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 0


async def test_service_rejects_non_admin_without_api_guard(auth_api):
    _, auth, database = auth_api
    account = await auth.store.find_user("admin")
    principal = Principal(replace(account, role="user"), uuid4())
    service = CatalogService(CatalogStore(database))
    with pytest.raises(AuthError):
        await service.candidates(principal, "", None, 12, 0)
    with pytest.raises(AuthError):
        await service.products(principal, 12, 0)
    with pytest.raises(AuthError):
        await service.import_product(principal, ImportProduct(**payload()))


def test_fixture_is_explicitly_simulated_and_has_unique_identifiers():
    assert len(CATALOG) == 24
    assert len({row.id for row in CATALOG}) == len(CATALOG)
    assert all(row.is_demo and row.source == "mock_catalog_v1" for row in CATALOG)
    assert all("模拟" not in row.name for row in CATALOG)

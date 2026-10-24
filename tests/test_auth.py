from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.api.v1.auth import cookie_name
from backend.config import AuthSettings, Settings
from backend.db.auth_store import AuthStore
from backend.db.session import Database
from backend.main import create_app
from backend.models.orm import Base, LoginSession, User

PASSWORD = "Test-password-2026"
ORIGIN = "http://test"


@pytest_asyncio.fixture
async def auth_api(tmp_path):
    database = Database(create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}"))
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        _env_file=None,
        auth=AuthSettings(
            signing_key=SecretStr("auth-test-key-" * 4),
            allowed_origins=(ORIGIN,),
        ),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        service = app.state.auth_service
        service.store = AuthStore(database)
        for name, role in (("alice", "user"), ("bob", "staff"), ("admin", "admin")):
            await service.create_user(name, name.title(), role, PASSWORD)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield client, service, database
    await database.close()


async def login(client, username="alice", **kwargs):
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}, **kwargs
    )
    assert response.status_code == 200, response.text
    return response.json()


def headers(tokens):
    return {
        "Authorization": f"Bearer {tokens['access_token']}",
        "X-Session-ID": tokens["session_id"],
    }


async def test_login_masks_refresh_and_password_and_checks_me(auth_api):
    client, _, _ = auth_api
    response = await client.post(
        "/api/auth/login", json={"username": "ALICE", "password": PASSWORD}
    )
    assert response.status_code == 200
    tokens = response.json()
    assert "refresh_token" not in tokens
    assert "password_hash" not in tokens["user"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/auth" in cookie
    assert response.headers["cache-control"] == "no-store"
    me = await client.get("/api/auth/me", headers=headers(tokens))
    assert me.json()["username"] == "alice"
    assert me.headers["x-auth-session"] == tokens["session_id"]


async def test_two_accounts_in_shared_cookie_jar_remain_independent(auth_api):
    client, _, _ = auth_api
    alice, bob = await login(client), await login(client, "bob")
    assert alice["session_id"] != bob["session_id"]
    assert len(list(client.cookies.jar)) == 2
    for tokens, name in ((alice, "alice"), (bob, "bob")):
        refreshed = await client.post(
            "/api/auth/refresh", headers={"X-Session-ID": tokens["session_id"]}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["user"]["username"] == name
    await client.post("/api/auth/logout", headers=headers(alice))
    assert (await client.get("/api/auth/me", headers=headers(alice))).status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(bob))).status_code == 200


async def test_refresh_rotation_reuse_revokes_only_that_family(auth_api):
    client, _, _ = auth_api
    alice, bob = await login(client), await login(client, "bob")
    name = cookie_name(UUID(alice["session_id"]))
    old_refresh = client.cookies.get(name)
    renewed = await client.post("/api/auth/refresh", headers={"X-Session-ID": alice["session_id"]})
    assert renewed.status_code == 200
    assert client.cookies.get(name) != old_refresh
    replay = await client.post(
        "/api/auth/refresh",
        headers={
            "X-Session-ID": alice["session_id"],
            "Cookie": f"{name}={old_refresh}",
        },
    )
    assert replay.status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(renewed.json()))).status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(bob))).status_code == 200


async def test_switch_replaces_selected_session_without_affecting_other_tab(auth_api):
    client, _, _ = auth_api
    selected, separate = await login(client), await login(client)
    switched = await login(client, "bob", headers={"X-Session-ID": selected["session_id"]})
    assert (await client.get("/api/auth/me", headers=headers(selected))).status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(separate))).status_code == 200
    assert (await client.get("/api/auth/me", headers=headers(switched))).json()["username"] == "bob"


async def test_wrong_session_id_and_token_purpose_are_rejected(auth_api):
    client, _, _ = auth_api
    alice, bob = await login(client), await login(client, "bob")
    wrong = headers(alice) | {"X-Session-ID": bob["session_id"]}
    assert (await client.get("/api/auth/me", headers=wrong)).status_code == 401
    refresh = client.cookies.get(cookie_name(UUID(alice["session_id"])))
    assert (
        await client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    ).status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(bob))).status_code == 200


async def test_cookie_mutations_require_trusted_origin(auth_api):
    client, _, _ = auth_api
    tokens = await login(client)
    for origin in ("http://evil.example", "null"):
        result = await client.post(
            "/api/auth/refresh",
            headers={
                "Origin": origin,
                "X-Session-ID": tokens["session_id"],
            },
        )
        assert result.status_code == 403
    request = client.build_request("POST", "/api/auth/logout", headers=headers(tokens))
    del request.headers["Origin"]
    assert (await client.send(request)).status_code == 403
    assert (await client.get("/api/auth/me", headers=headers(tokens))).status_code == 200


async def test_admin_access_is_checked_in_backend(auth_api):
    client, _, _ = auth_api
    assert (await client.get("/api/admin/users")).status_code == 401
    for name in ("alice", "bob"):
        tokens = await login(client, name)
        assert (await client.get("/api/admin/users", headers=headers(tokens))).status_code == 403
    admin = await login(client, "admin")
    result = await client.post(
        "/api/admin/users",
        headers=headers(admin),
        json={
            "username": "created",
            "display_name": "Created",
            "role": "user",
            "password": PASSWORD,
        },
    )
    assert result.status_code == 201
    assert "password" not in result.text
    assert (await client.get("/api/admin/users?limit=2", headers=headers(admin))).status_code == 200


async def test_password_change_revokes_all_user_sessions(auth_api):
    client, _, _ = auth_api
    first, second, bob = await login(client), await login(client), await login(client, "bob")
    changed = await client.post(
        "/api/auth/password",
        headers=headers(first),
        json={
            "current_password": PASSWORD,
            "new_password": "New-testing-password-2026",
        },
    )
    assert changed.status_code == 204
    for tokens in (first, second):
        assert (await client.get("/api/auth/me", headers=headers(tokens))).status_code == 401
    assert (await client.get("/api/auth/me", headers=headers(bob))).status_code == 200
    assert (
        await client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "alice", "password": "New-testing-password-2026"}
        )
    ).status_code == 200


async def test_disabled_user_invalidates_access(auth_api):
    client, _, database = auth_api
    tokens = await login(client)
    async with database.session() as session:
        user = await session.scalar(select(User).where(User.username == "alice"))
        user.is_active = False
    assert (await client.get("/api/auth/me", headers=headers(tokens))).status_code == 401


async def test_long_session_has_an_absolute_expiration(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    async with database.session() as session:
        login_row = await session.get(LoginSession, UUID(tokens["session_id"]))
        login_row.expires_at = datetime.now(UTC) + timedelta(hours=1)
    refreshed = await client.post(
        "/api/auth/refresh", headers={"X-Session-ID": tokens["session_id"]}
    )
    claims = service.codec.decode(
        client.cookies.get(cookie_name(UUID(tokens["session_id"]))), "refresh"
    )
    assert claims["exp"] <= int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    assert refreshed.status_code == 200


async def test_login_rate_limit_is_bounded(auth_api):
    client, service, _ = auth_api
    for _ in range(20):
        await service.check_login_budget("127.0.0.1", "alice")
    result = await client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert result.status_code == 429
    assert result.headers["retry-after"] == "60"


async def test_saved_sessions_restore_after_losing_tab_storage(auth_api):
    client, _, _ = auth_api
    alice, bob = await login(client), await login(client, "bob")
    available = await client.post("/api/auth/sessions")
    assert available.status_code == 200
    assert {item["session_id"] for item in available.json()} == {
        alice["session_id"],
        bob["session_id"],
    }
    assert "access_token" not in available.text and "refresh_token" not in available.text
    restored = await client.post("/api/auth/refresh", headers={"X-Session-ID": alice["session_id"]})
    assert restored.json()["user"]["username"] == "alice"
    await client.post("/api/auth/logout", headers=headers(bob))
    remaining = await client.post("/api/auth/sessions")
    assert [item["user"]["username"] for item in remaining.json()] == ["alice"]


async def test_one_account_limit_does_not_block_another_account(auth_api):
    client, service, _ = auth_api
    for _ in range(20):
        await service.check_login_budget("127.0.0.1", "alice")
    assert (
        await client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    ).status_code == 429
    assert (
        await client.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
    ).status_code == 200

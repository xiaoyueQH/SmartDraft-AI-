import asyncio

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError

from backend.config import DatabaseSettings, Settings
from backend.dependencies import get_database
from backend.main import create_app


class StubDatabase:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.cancelled = False

    async def ping(self):
        if self.mode == "failed":
            raise OperationalError("SELECT 1", {}, Exception("private database details"))
        if self.mode == "slow":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


@pytest_asyncio.fixture
async def api():
    settings = Settings(_env_file=None, database=DatabaseSettings(health_timeout_seconds=0.03))
    app = create_app(settings)
    stub = StubDatabase()
    app.dependency_overrides[get_database] = lambda: stub
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, stub


async def test_live_and_ready(api):
    _, client, _ = api
    live = await client.get("/api/health/live")
    ready = await client.get("/api/health/ready")
    assert live.status_code == ready.status_code == 200
    assert live.json()["service"] == "supportflow"
    assert ready.json() == {"status": "ready", "checks": {"postgresql": "ok"}}
    assert live.headers["x-request-id"] != ready.headers["x-request-id"]
    assert ready.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("mode", ["failed", "slow"])
async def test_database_failure_keeps_api_live_and_details_private(api, mode):
    _, client, stub = api
    stub.mode = mode
    ready = await asyncio.wait_for(client.get("/api/health/ready"), timeout=1)
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "checks": {"postgresql": "unavailable"}}
    assert "private database details" not in ready.text
    assert (await client.get("/api/health/live")).status_code == 200
    if mode == "slow":
        assert stub.cancelled


async def test_error_envelope_and_request_id(api):
    app, client, _ = api

    @app.get("/test/failure")
    async def fail():
        raise RuntimeError("sensitive value must not reach the response")

    for path, expected in [("/missing", 404), ("/test/failure", 500)]:
        response = await client.get(path)
        assert response.status_code == expected
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "sensitive value" not in response.text


async def test_validation_does_not_echo_user_input(api):
    app, client, _ = api

    @app.get("/test/validate")
    async def validate(count: int):
        return {"count": count}

    response = await client.get("/test/validate", params={"count": "private-value"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "private-value" not in response.text


async def test_parallel_requests_have_distinct_request_ids(api):
    _, client, _ = api
    responses = await asyncio.gather(*(client.get("/api/health/live") for _ in range(40)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.headers["x-request-id"] for response in responses}) == 40


async def test_lifecycle_recreates_resources():
    app = create_app(Settings(_env_file=None))
    async with app.router.lifespan_context(app):
        first = app.state.database
    assert not hasattr(app.state, "database")
    async with app.router.lifespan_context(app):
        assert app.state.database is not first
    assert not hasattr(app.state, "database")

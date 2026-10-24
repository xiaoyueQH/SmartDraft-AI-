import asyncio

import httpx

from backend.config import AppSettings, Settings
from backend.main import create_app


async def test_overload_is_bounded_and_liveness_remains_available():
    app = create_app(
        Settings(_env_file=None, app=AppSettings(request_capacity=1, request_wait_seconds=0.03))
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    @app.get("/test/slow")
    async def slow():
        entered.set()
        await release.wait()
        return {"ok": True}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.get("/test/slow"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                busy = await asyncio.wait_for(client.get("/test/slow"), timeout=1)
                assert busy.status_code == 503
                assert busy.json()["error"]["code"] == "SERVER_BUSY"
                assert busy.headers["retry-after"] == "1"
                assert busy.json()["request_id"] == busy.headers["x-request-id"]
                assert (await client.get("/api/health/live")).status_code == 200
            finally:
                release.set()
                assert (await first).status_code == 200
            assert (await client.get("/test/slow")).status_code == 200


async def test_cancellation_returns_execution_slot():
    app = create_app(Settings(_env_file=None, app=AppSettings(request_capacity=1)))
    entered = asyncio.Event()

    @app.get("/test/wait")
    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task = asyncio.create_task(client.get("/test/wait"))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert (await client.get("/missing")).status_code == 404

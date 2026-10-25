# ruff: noqa: F811
import asyncio
import os
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError
from sqlalchemy import select
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import project

from backend.config import CacheSettings
from backend.db.cache import ReadCache
from backend.db.project_store import ProjectStore
from backend.dependencies import get_read_cache
from backend.models.projects import ProjectError, ProjectRun, SendMessage, StartConsultation


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.failed = False

    async def get(self, key):
        if self.failed:
            raise ConnectionError("private connection detail")
        value, until = self.values.get(key, (None, 0))
        return value if until > monotonic() else None

    async def set(self, key, value, nx=False, ex=None, px=None):
        if self.failed:
            raise ConnectionError("private connection detail")
        if nx and await self.get(key) is not None:
            return None
        self.values[key] = (value, monotonic() + (ex if ex else px / 1000))
        return True

    async def ping(self):
        return not self.failed

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def cache():
    cache = ReadCache(CacheSettings(enabled=False, load_wait_seconds=1))
    cache.settings = cache.settings.model_copy(update={"enabled": True})
    cache.client = MemoryRedis()
    yield cache
    await cache.close()


async def test_concurrent_misses_share_one_loader_and_expire(cache):
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {"count": calls}

    results = await asyncio.gather(
        *(cache.get_or_load("run", "same", load, 0.1) for _ in range(24))
    )
    assert results == [{"count": 1}] * 24 and calls == 1
    assert not cache.locks
    await asyncio.sleep(0.11)
    assert await cache.get_or_load("run", "same", load, 0.1) == {"count": 2}


async def test_invalidation_cannot_be_undone_by_an_older_fill(cache):
    started, proceed = asyncio.Event(), asyncio.Event()

    async def old():
        started.set()
        await proceed.wait()
        return {"count": 1}

    task = asyncio.create_task(cache.get_or_load("summary", "admin", old, 15))
    await started.wait()
    await cache.invalidate_summary()
    proceed.set()
    assert await task == {"count": 1}

    async def current():
        return {"count": 2}

    assert await cache.get_or_load("summary", "admin", current, 15) == {"count": 2}


async def test_outage_opens_circuit_and_keeps_database_reads_available(cache, caplog):
    cache.client.failed = True

    async def load():
        return {"count": 3}

    for _ in range(3):
        assert await cache.get_or_load("summary", "admin", load, 15) == {"count": 3}
    assert cache.counters["errors"] == 1
    assert (await cache.status())["circuit_open"]
    assert "private connection detail" not in caplog.text


async def test_fallback_concurrency_is_bounded_and_cancellation_releases_slots(cache):
    cache.settings = cache.settings.model_copy(update={"load_wait_seconds": 0.02})
    cache.loads = asyncio.Semaphore(1)
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cache.get_or_load("run", "first", hold, 3))
    await started.wait()
    with pytest.raises(ProjectError) as error:
        await cache.get_or_load("run", "second", hold, 3)
    assert error.value.status == 503
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cache.locks and not cache.loads.locked()


async def test_snapshot_version_and_access_are_checked_before_cache(auth_api, cache):
    client, auth, database = auth_api
    client._transport.app.dependency_overrides[get_read_cache] = lambda: cache
    tokens = await login(client)
    identity = headers(tokens)
    principal = await auth.authenticate(tokens["access_token"])
    p = await project(client, identity)
    pid = UUID(p["id"])
    store = ProjectStore(database, cache)
    run, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content="业务信息")
    )
    rid = UUID(run["id"])
    url = f"/api/projects/{pid}/runs/{rid}"
    for _ in range(2):
        assert (await client.get(url, headers=identity)).json()["events"] == []
    assert cache.counters["hits"] == 1
    other = headers(await login(client, "bob"))
    assert (await client.get(url, headers=other)).status_code == 404
    assert cache.counters["hits"] == 1
    await store.event(rid, {"type": "node", "summary": "处理业务"})
    assert len((await client.get(url, headers=identity)).json()["events"]) == 1
    await store.finish(principal, pid, rid, 0, "已整理", [], defer_completion=True)
    assert (await client.get(url, headers=identity)).json()["status"] == "running"
    await store.checkpoint_ready(rid)
    assert (await client.get(url, headers=identity)).json()["status"] == "completed"
    async with database.session() as session:
        version = await session.scalar(select(ProjectRun.cache_version).where(ProjectRun.id == rid))
    assert version == 4
    for expire in (False, True):
        run, _ = await store.start_run(
            principal, pid, SendMessage(request_id=uuid4(), content="后续信息")
        )
        rid = UUID(run["id"])
        next_url = f"/api/projects/{pid}/runs/{rid}"
        assert (await client.get(next_url, headers=identity)).json()["status"] == "running"
        if expire:
            async with database.session() as session:
                row = await session.get(ProjectRun, rid)
                row.created_at = datetime.now(UTC) - timedelta(seconds=181)
        else:
            await store.fail(rid, "处理失败")
        assert (await client.get(next_url, headers=identity)).json()["status"] == "failed"
    current = (await client.get(f"/api/projects/{pid}", headers=identity)).json()["project"]
    deleted = await client.post(
        "/api/projects/records/actions",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "action": "trash",
            "targets": [{"id": str(pid), "expected_version": current["version"]}],
        },
    )
    assert deleted.status_code == 200
    assert (await client.get(url, headers=identity)).status_code == 404


async def test_summary_cache_invalidates_after_business_commit(auth_api, cache):
    client, _, _ = auth_api
    client._transport.app.dependency_overrides[get_read_cache] = lambda: cache
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    url = "/api/admin/projects/summary"
    for _ in range(2):
        assert (await client.get(url, headers=admin)).json()["consultations"] == 0
    assert cache.counters["hits"] == 1
    assert (await client.get(url, headers=customer)).status_code == 403
    await project(client, customer)
    assert (await client.get(url, headers=admin)).json()["consultations"] == 1
    assert (await client.get("/api/admin/cache", headers=customer)).status_code == 403
    assert (await client.get("/api/admin/cache", headers=admin)).json()["enabled"]


@pytest.mark.skipif(
    os.getenv("SUPPORTFLOW_TEST_REDIS") != "1", reason="Enable the local Redis integration check"
)
async def test_real_redis_cache_and_invalidation():
    cache = ReadCache(CacheSettings(enabled=True, prefix="verification:" + uuid4().hex))
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return {"count": calls}

    try:
        assert (await cache.status())["available"]
        assert await cache.get_or_load("summary", "admin", load, 2) == {"count": 1}
        assert await cache.get_or_load("summary", "admin", load, 2) == {"count": 1}
        await cache.invalidate_summary()
        assert await cache.get_or_load("summary", "admin", load, 2) == {"count": 2}
    finally:
        keys = [key async for key in cache.client.scan_iter(match=cache.settings.prefix + ":*")]
        if keys:
            await cache.client.delete(*keys)
        await cache.close()


@pytest.mark.skipif(
    os.getenv("SUPPORTFLOW_TEST_REDIS") != "1" or os.getenv("SUPPORTFLOW_TEST_POSTGRES") != "1",
    reason="Enable isolated PostgreSQL and Redis checks",
)
async def test_real_postgres_redis_parallel_snapshots_stay_isolated(postgres_auth):
    cache = ReadCache(
        CacheSettings(enabled=True, prefix="verification:" + uuid4().hex, load_wait_seconds=1)
    )
    auth = postgres_auth
    principal = await auth.authenticate(
        (await auth.login("alice", "Postgres-test-password")).access_token
    )
    other = await auth.authenticate(
        (await auth.login("bob", "Postgres-test-password")).access_token
    )
    store = ProjectStore(auth.store.database, cache)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    row, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content="客户原话")
    )
    rid = UUID(row["id"])
    try:
        result = await asyncio.gather(*(store.run(principal, pid, rid) for _ in range(24)))
        assert all(item["id"] == str(rid) for item in result)
        assert cache.counters["fills"] == 1 and cache.counters["hits"] == 23
        for _ in range(2):
            with pytest.raises(ProjectError) as error:
                await store.run(other, pid, rid)
            assert error.value.status == 404
        assert cache.counters["hits"] == 23
        await store.fail(rid, "本次执行结束")
        assert (await store.run(principal, pid, rid))["status"] == "failed"
    finally:
        keys = [key async for key in cache.client.scan_iter(match=cache.settings.prefix + ":*")]
        if keys:
            await cache.client.delete(*keys)
        await cache.close()

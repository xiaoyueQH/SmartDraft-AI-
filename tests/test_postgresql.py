import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from backend.config import AuthSettings, get_settings
from backend.core.security import JwtCodec, PasswordWork
from backend.db.auth_store import AuthStore
from backend.db.session import Database, create_database
from backend.models.auth import AuthError
from backend.models.orm import Base
from backend.services.auth import AuthService, TokenBundle

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1",
    reason="Set SUPPORTFLOW_TEST_POSTGRES=1 to test the local PostgreSQL instance",
)


@pytest_asyncio.fixture
async def postgres():
    settings = get_settings().database.model_copy(
        update={"pool_size": 2, "max_overflow": 1, "pool_timeout_seconds": 0.15}
    )
    database = create_database(settings)
    schema = f"acceptance_{uuid4().hex}"
    try:
        async with database.session() as session:
            await session.execute(text(f"CREATE SCHEMA {schema}"))
            await session.execute(
                text(f"CREATE TABLE {schema}.records (id INTEGER PRIMARY KEY, value TEXT)")
            )
        yield database, schema
    finally:
        try:
            async with database.session() as session:
                await session.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        finally:
            await database.close()


async def test_real_postgres_parallel_commits(postgres):
    database, schema = postgres

    async def insert(record_id):
        async with database.session() as session:
            await session.execute(
                text(f"INSERT INTO {schema}.records VALUES (:id, 'parallel')"),
                {"id": record_id},
            )

    await asyncio.gather(*(insert(index) for index in range(32)))
    async with database.session() as session:
        count = await session.scalar(text(f"SELECT COUNT(*) FROM {schema}.records"))
    assert count == 32


async def test_real_postgres_cancellation_rolls_back(postgres):
    database, schema = postgres
    written = asyncio.Event()

    async def insert_then_wait():
        async with database.session() as session:
            await session.execute(text(f"INSERT INTO {schema}.records VALUES (1, 'cancelled')"))
            written.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(insert_then_wait())
    await asyncio.wait_for(written.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with database.session() as session:
        assert await session.scalar(text(f"SELECT COUNT(*) FROM {schema}.records")) == 0


async def test_real_postgres_pool_exhaustion_has_bounded_wait(postgres):
    database, _ = postgres
    acquired = asyncio.Queue()
    release = asyncio.Event()

    async def hold_connection():
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
            acquired.put_nowait(True)
            await release.wait()

    holders = [asyncio.create_task(hold_connection()) for _ in range(3)]
    try:
        for _ in holders:
            await asyncio.wait_for(acquired.get(), timeout=3)
        assert database.engine.pool.checkedout() == 3
        with pytest.raises(PoolTimeoutError):
            await asyncio.wait_for(database.ping(), timeout=1)
    finally:
        release.set()
        await asyncio.gather(*holders)
    await database.ping()


async def test_real_postgres_query_timeout_recovers_connection():
    settings = get_settings().database.model_copy(update={"query_timeout_seconds": 0.05})
    database = create_database(settings)
    try:
        with pytest.raises((DBAPIError, TimeoutError)):
            async with database.session() as session:
                await asyncio.wait_for(session.execute(text("SELECT pg_sleep(1)")), timeout=2)
        await database.ping()
    finally:
        await database.close()


@pytest_asyncio.fixture
async def postgres_auth(postgres):
    _, schema = postgres
    # 业务一致性测试保留 3 个连接，使用正常等待预算；150ms 专门用于上面的耗尽测试。
    settings = get_settings().database.model_copy(update={"pool_size": 2, "max_overflow": 1})
    database = create_database(settings)
    mapped = Database(database.engine.execution_options(schema_translate_map={None: schema}))
    async with mapped.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = AuthSettings(signing_key=SecretStr("postgres-auth-test-key-" * 3))
    passwords = PasswordWork(2)
    await passwords.initialize()
    service = AuthService(
        AuthStore(mapped),
        JwtCodec(settings, settings.signing_key.get_secret_value()),
        passwords,
        settings,
    )
    await service.create_user("alice", "Alice", "user", "Postgres-test-password")
    await service.create_user("bob", "Bob", "user", "Postgres-test-password")
    try:
        yield service
    finally:
        await passwords.close()
        await database.close()


async def test_real_postgres_concurrent_refresh_detects_replay(postgres_auth):
    service = postgres_auth
    alice = await service.login("alice", "Postgres-test-password")
    bob = await service.login("bob", "Postgres-test-password")
    results = await asyncio.gather(
        service.refresh(alice.session_id, alice.refresh_token),
        service.refresh(alice.session_id, alice.refresh_token),
        return_exceptions=True,
    )
    assert sum(isinstance(result, TokenBundle) for result in results) == 1
    assert sum(isinstance(result, AuthError) for result in results) == 1
    accepted = next(result for result in results if isinstance(result, TokenBundle))
    with pytest.raises(AuthError):
        await service.authenticate(accepted.access_token)
    assert (await service.authenticate(bob.access_token)).account.username == "bob"


async def test_real_postgres_refresh_logout_race_stays_revoked(postgres_auth):
    service = postgres_auth
    alice = await service.login("alice", "Postgres-test-password")
    results = await asyncio.gather(
        service.refresh(alice.session_id, alice.refresh_token),
        service.logout(alice.session_id, alice.refresh_token, alice.access_token),
        return_exceptions=True,
    )
    with pytest.raises(AuthError):
        await service.authenticate(alice.access_token)
    for result in results:
        if isinstance(result, TokenBundle):
            with pytest.raises(AuthError):
                await service.authenticate(result.access_token)


async def test_real_postgres_parallel_account_reads_stay_isolated(postgres_auth):
    service = postgres_auth
    alice = await service.login("alice", "Postgres-test-password")
    bob = await service.login("bob", "Postgres-test-password")
    pairs = [(alice, "alice"), (bob, "bob")] * 12
    results = await asyncio.gather(
        *(service.authenticate(bundle.access_token) for bundle, _ in pairs)
    )
    assert [result.account.username for result in results] == [name for _, name in pairs]


async def test_real_postgres_login_budget_is_shared_and_atomic(postgres_auth):
    service = postgres_auth
    second = AuthStore(service.store.database)
    stores = [service.store, second] * 4
    results = await asyncio.gather(
        *(store.check_login_budget("shared-proxy", "alice", 3) for store in stores),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 3
    assert sum(isinstance(result, AuthError) and result.status == 429 for result in results) == 5
    await second.check_login_budget("shared-proxy", "bob", 3)


async def test_real_postgres_password_change_wins_over_refresh(postgres_auth):
    service = postgres_auth
    first = await service.login("alice", "Postgres-test-password")
    second = await service.login("alice", "Postgres-test-password")
    bob = await service.login("bob", "Postgres-test-password")
    principal = await service.authenticate(first.access_token)
    results = await asyncio.gather(
        service.change_password(principal, "Postgres-test-password", "Changed-postgres-password"),
        service.refresh(second.session_id, second.refresh_token),
        return_exceptions=True,
    )
    assert results[0] is None
    for bundle in (first, second, *(item for item in results if isinstance(item, TokenBundle))):
        with pytest.raises(AuthError):
            await service.authenticate(bundle.access_token)
    assert (await service.authenticate(bob.access_token)).account.username == "bob"

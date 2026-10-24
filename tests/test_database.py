import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db.session import Database


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transactions.db'}")
    db = Database(engine)
    async with db.session() as session:
        await session.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)"))
    yield db
    await db.close()


async def record_count(database):
    async with database.session() as session:
        return await session.scalar(text("SELECT COUNT(*) FROM records"))


async def test_success_commits_and_failure_rolls_back(database):
    async with database.session() as session:
        await session.execute(text("INSERT INTO records VALUES (1, 'committed')"))
    with pytest.raises(RuntimeError):
        async with database.session() as session:
            await session.execute(text("INSERT INTO records VALUES (2, 'rolled back')"))
            raise RuntimeError("abort transaction")
    assert await record_count(database) == 1


async def test_sql_error_does_not_poison_next_transaction(database):
    with pytest.raises(IntegrityError):
        async with database.session() as session:
            await session.execute(text("INSERT INTO records VALUES (1, 'first')"))
            await session.execute(text("INSERT INTO records VALUES (1, 'duplicate')"))
    assert await record_count(database) == 0
    async with database.session() as session:
        await session.execute(text("INSERT INTO records VALUES (2, 'next')"))
    assert await record_count(database) == 1


async def test_cancelled_request_rolls_back(database):
    written = asyncio.Event()
    never_complete = asyncio.Event()

    async def write_and_wait():
        async with database.session() as session:
            await session.execute(text("INSERT INTO records VALUES (1, 'cancelled')"))
            written.set()
            await never_complete.wait()

    task = asyncio.create_task(write_and_wait())
    await asyncio.wait_for(written.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await record_count(database) == 0


async def test_concurrent_operations_use_independent_transactions(database):
    sessions = []

    async def insert_record(record_id):
        async with database.session() as session:
            sessions.append(session)
            await session.execute(
                text("INSERT INTO records VALUES (:id, 'parallel')"), {"id": record_id}
            )

    await asyncio.gather(*(insert_record(index) for index in range(8)))
    assert len({id(session) for session in sessions}) == 8
    assert await record_count(database) == 8

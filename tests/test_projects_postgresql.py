# ruff: noqa: F811
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.project_store import ProjectStore
from backend.models.projects import (
    ManageRecords,
    Project,
    ProjectError,
    ProjectRecordEvent,
    ProjectRun,
    RecordTarget,
    SendMessage,
    StartConsultation,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires local PostgreSQL"
)


async def identity(auth, name="alice"):
    return await auth.authenticate((await auth.login(name, "Postgres-test-password")).access_token)


async def test_parallel_project_creation_is_idempotent_and_owner_reads_are_isolated(postgres_auth):
    auth = postgres_auth
    alice, bob = await identity(auth), await identity(auth, "bob")
    store = ProjectStore(auth.store.database)
    command = StartConsultation(request_id=uuid4())
    results = await asyncio.gather(*(store.start_consultation(alice, command) for _ in range(12)))
    assert len({row["id"] for row in results}) == 1
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
    reads = await asyncio.gather(*(store.projects(who) for who in [alice, bob] * 12))
    assert [len(page["items"]) for page in reads] == [1, 0] * 12


async def test_cross_process_project_agent_admission_uses_database_mutex(postgres_auth):
    auth = postgres_auth
    principal = await identity(auth)
    store = ProjectStore(auth.store.database)
    project = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    stores = [store, ProjectStore(auth.store.database)] * 4
    results = await asyncio.gather(
        *(
            other.start_run(
                principal, UUID(project["id"]), SendMessage(request_id=uuid4(), content="整理需求")
            )
            for other in stores
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) and result[1] for result in results) == 1
    assert (
        sum(
            isinstance(result, ProjectError) and result.code == "PROJECT_BUSY" for result in results
        )
        == 7
    )


async def test_poll_expiry_waits_for_row_lock_and_preserves_completed_result(postgres_auth):
    auth = postgres_auth
    principal = await identity(auth)
    store = ProjectStore(auth.store.database)
    project = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    run, _ = await store.start_run(
        principal, UUID(project["id"]), SendMessage(request_id=uuid4(), content="整理需求")
    )
    run_id = UUID(run["id"])
    async with auth.store.database.session() as session:
        row = await session.get(ProjectRun, run_id)
        row.created_at = datetime.now(UTC) - timedelta(seconds=181)
    async with auth.store.database.session() as session:
        row = await session.scalar(
            select(ProjectRun).where(ProjectRun.id == run_id).with_for_update()
        )
        row.status, row.answer = "completed", "已保存，不能被过期轮询覆盖"
        await session.flush()
        poll = asyncio.create_task(store.run(principal, UUID(project["id"]), run_id))
        await asyncio.sleep(0.05)
        assert not poll.done()
    result = await poll
    assert result["status"] == "completed" and result["answer"] == "已保存，不能被过期轮询覆盖"


async def test_parallel_batch_delete_and_restore_are_idempotent(postgres_auth):
    auth = postgres_auth
    principal = await identity(auth)
    store = ProjectStore(auth.store.database)
    records = [
        await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
        for _ in range(2)
    ]
    command = ManageRecords(
        request_id=uuid4(),
        action="trash",
        targets=[RecordTarget(id=row["id"], expected_version=row["version"]) for row in records],
    )
    results = await asyncio.gather(*(store.manage_records(principal, command) for _ in range(8)))
    assert all(result == results[0] for result in results)
    assert (await store.projects(principal))["items"] == []
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRecordEvent)) == 2
    restore = ManageRecords(
        request_id=uuid4(),
        action="restore",
        targets=[
            RecordTarget(id=row["id"], expected_version=row["version"])
            for row in results[0]["items"]
        ],
    )
    await asyncio.gather(*(store.manage_records(principal, restore) for _ in range(8)))
    assert len((await store.projects(principal))["items"]) == 2
    async with auth.store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRecordEvent)) == 4


async def test_agent_start_racing_with_delete_never_runs_on_deleted_record(postgres_auth):
    auth = postgres_auth
    principal = await identity(auth)
    store = ProjectStore(auth.store.database)
    record = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    results = await asyncio.gather(
        store.start_run(
            principal, UUID(record["id"]), SendMessage(request_id=uuid4(), content="整理业务")
        ),
        store.manage_records(
            principal,
            ManageRecords(
                request_id=uuid4(),
                action="trash",
                targets=[RecordTarget(id=record["id"], expected_version=record["version"])],
            ),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ProjectError) for result in results) == 1
    async with auth.store.database.session() as session:
        project = await session.get(Project, UUID(record["id"]))
        running = await session.scalar(
            select(func.count())
            .select_from(ProjectRun)
            .where(ProjectRun.project_id == project.id, ProjectRun.status == "running")
        )
        assert not (project.deleted_at and running)

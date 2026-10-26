# ruff: noqa: F811
import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_postgresql import postgres, postgres_auth  # noqa: F401

from backend.db.project_admin_store import ProjectAdminStore
from backend.db.project_store import ProjectStore
from backend.models.projects import (
    CreateItem,
    ItemAction,
    ManageRecords,
    Project,
    ProjectError,
    ProjectOperation,
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


async def test_postgres_review_migration_and_full_business_lifecycle(postgres_auth):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    auth = postgres_auth
    await auth.create_user("reviewer", "负责人", "admin", "Postgres-test-password")
    customer, admin = await identity(auth), await identity(auth, "reviewer")
    database = auth.store.database
    store = ProjectStore(database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    item = await store.create_item(
        customer,
        pid,
        CreateItem(
            request_id=uuid4(),
            kind="specification",
            title="会议室需求书",
            content="员工预约会议室，前台维护空闲时间。",
            base_revision=0,
        ),
    )
    # Reproduce the deployed pre-migration width and upgrade it with an existing receipt.
    async with database.engine.begin() as connection:

        def upgrade(sync):
            schema = sync.get_execution_options()["schema_translate_map"][None]
            sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            sync.execute(
                text("ALTER TABLE project_operations ALTER COLUMN action TYPE VARCHAR(40)")
            )
            migration = importlib.import_module(
                "backend.db.migrations.versions.0016_project_operation_action"
            )
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()

        await connection.run_sync(upgrade)

    async def act(who, row, action, **extra):
        command = ItemAction(
            request_id=uuid4(), expected_version=row["version"], action=action, **extra
        )
        result = await store.act(who, pid, UUID(row["id"]), command)
        assert await store.act(who, pid, UUID(row["id"]), command) == result
        return result["item"]

    item = await act(customer, item, "submit")
    assert item["status"] == "submitted"
    queue = await ProjectAdminStore(database).items(
        admin, category="documents", status="pending_review"
    )
    assert [entry["id"] for entry in queue["items"]] == [item["id"]]
    assert (await store.detail(customer, pid))["project"]["revision"] == 0
    item = await act(customer, item, "confirm")
    assert item["customer_confirmed"] and not item["staff_confirmed"]
    item = await act(admin, item, "confirm")
    assert item["published_revision"] == 1
    snapshot = await store.detail(customer, pid)
    assert snapshot["project"]["phase"] == "project" and snapshot["project"]["revision"] == 1
    change = await store.create_item(
        customer,
        pid,
        CreateItem(
            request_id=uuid4(),
            kind="change",
            title="增加邮件提醒",
            content=item["content"] + "增加邮件提醒。",
            base_revision=1,
        ),
    )
    for who, action in ((customer, "submit"), (admin, "confirm"), (customer, "confirm")):
        change = await act(who, change, action)
    assert change["published_revision"] == 2
    assert (await store.item(customer, pid, UUID(item["id"])))["content"] == item["content"]
    ticket = await store.create_item(
        customer,
        pid,
        CreateItem(
            request_id=uuid4(),
            kind="bug",
            title="邮件未收到",
            content="预约成功但没有提醒。",
            base_revision=2,
        ),
    )
    for who, action, note in (
        (customer, "submit", ""),
        (admin, "start", ""),
        (admin, "supplement", "已检查通知配置"),
        (admin, "deliver", "已修复并提供验证记录"),
        (customer, "accept", "复测通过"),
    ):
        ticket = await act(who, ticket, action, note=note)
    assert ticket["status"] == "closed"
    async with database.session() as session:
        actions = list(
            await session.scalars(
                select(ProjectOperation.action).where(ProjectOperation.project_id == pid)
            )
        )
    assert f"submit:{item['id']}" in actions and f"supplement:{ticket['id']}" in actions
    assert max(map(len, actions)) > 40
    async with database.session() as session:
        session.add_all(
            [
                ProjectOperation(
                    actor_id=customer.account.id,
                    project_id=pid,
                    request_id=uuid4(),
                    fingerprint="a" * 64,
                    action="create_item",
                    result={"id": str(uuid4())},
                    created_at=datetime.now(UTC),
                )
                for _ in range(65)
            ]
        )
    history = await store.activity(customer, pid, UUID(item["id"]))
    assert len(history) == 4 and any(entry["action"] == "create_item" for entry in history)
    with pytest.raises(ProjectError, match="资料不存在"):
        await store.activity(customer, pid, uuid4())


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

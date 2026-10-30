# ruff: noqa: F811
import asyncio
import os
import sys
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import draft, project

from backend.config import AgentSettings, QueueSettings, RecoverySettings, Settings
from backend.core.job_context import JobLease, job_lease
from backend.db.conversation_store import ConversationStore
from backend.db.job_store import JobStore, clock
from backend.db.project_store import append_message
from backend.db.review_store import ReviewStore
from backend.models.jobs import AgentJob
from backend.models.projects import (
    Project,
    ProjectError,
    ProjectMessage,
    ProjectRun,
    SendMessage,
    StartConsultation,
)
from backend.models.review import DocumentReview, StartReview
from backend.services.job_worker import JobWorker, reconcile_jobs

PG = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Real PG required"
)


async def scenario(auth_api, *, queue=None):
    client, service, database = auth_api
    tokens = await login(client)
    principal = await service.authenticate(tokens["access_token"])
    p = await project(client, headers(tokens))
    store = ConversationStore(database, queue=queue)
    data = SendMessage(request_id=uuid4(), content="先聊业务，不生成需求")
    run, _ = await store.start_run(principal, UUID(p["id"]), data)
    return store, principal, UUID(p["id"]), data, run


async def test_request_and_job_are_atomic_idempotent_and_survive_new_consumer(auth_api):
    store, actor, pid, data, run = await scenario(auth_api)
    replay, created = await store.start_run(actor, pid, data)
    assert not created and replay["id"] == run["id"]
    async with store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(AgentJob)) == 1
        assert await session.scalar(select(func.count()).select_from(ProjectMessage)) == 1
        row = await session.get(ProjectRun, UUID(run["id"]))
        row.created_at = clock() - timedelta(minutes=4)
    # A queued execution is not an abandoned 180-second legacy execution.
    assert (await store.run(actor, pid, UUID(run["id"])))["status"] == "running"
    first = await JobStore(store.database).claim("new-process", 4)
    assert first.target_id == UUID(run["id"]) and first.session_id == actor.session_id
    assert first.attempts == 1 and first.worker_id == "new-process"
    assert await JobStore(store.database).claim("another-process", 4) is None


async def test_full_queue_rolls_back_user_message_and_business_run(auth_api):
    queue = QueueSettings(capacity=1, per_user=1)
    store, actor, _, _, _ = await scenario(auth_api, queue=queue)
    p = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
    with pytest.raises(ProjectError, match="等待处理"):
        await store.start_run(
            actor, UUID(p["id"]), SendMessage(request_id=uuid4(), content="第二条")
        )
    async with store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(AgentJob)) == 1
        assert await session.scalar(select(func.count()).select_from(ProjectRun)) == 1
        assert await session.scalar(select(func.count()).select_from(ProjectMessage)) == 1


async def test_expired_lease_fences_late_database_and_checkpoint_writes(auth_api):
    store, actor, pid, _, run = await scenario(auth_api)
    job = await JobStore(store.database).claim("lost-worker", 4)
    async with store.database.session() as session:
        row = await session.get(AgentJob, job.id)
        row.lease_until = clock() - timedelta(seconds=1)
    token = job_lease.set(JobLease(job.id, job.token))
    try:
        with pytest.raises(ProjectError, match="旧执行不能继续保存"):
            await store.event(UUID(run["id"]), {"summary": "不应保存的晚到结果"})
        from backend.db.checkpoints import DatabaseCheckpointer

        with pytest.raises(ProjectError, match="旧执行不能继续保存"):
            await DatabaseCheckpointer(store.database, pid).aget_tuple(
                {"configurable": {"thread_id": "late"}}
            )
    finally:
        job_lease.reset(token)
    await reconcile_jobs(store.database, None, RecoverySettings(), QueueSettings())
    assert (await store.run(actor, pid, UUID(run["id"])))["status"] == "failed"
    async with store.database.session() as session:
        row = await session.get(AgentJob, job.id)
        assert row.settled and row.status == "failed"
        assert "晚到结果" not in str((await session.get(ProjectRun, UUID(run["id"]))).events)
    assert not await JobStore(store.database).finish(job.id, job.token, "completed")


async def test_expired_owner_with_committed_receipt_does_not_replay_model(auth_api):
    store, actor, pid, _, run = await scenario(auth_api)
    job = await JobStore(store.database).claim("killed-after-commit", 4)
    async with store.database.session() as session:
        p = await session.get(Project, pid)
        p.consultation = {"last_run_id": run["id"]}
        append_message(session, p, "已经保存的完整回答", "ai", run_id=UUID(run["id"]))
        row = await session.get(AgentJob, job.id)
        row.lease_until = clock() - timedelta(seconds=1)
    await reconcile_jobs(store.database, None, RecoverySettings(), QueueSettings())
    await reconcile_jobs(store.database, None, RecoverySettings(), QueueSettings())
    result = await store.run(actor, pid, UUID(run["id"]))
    assert result["status"] == "completed" and result["answer"] == "已经保存的完整回答"
    async with store.database.session() as session:
        assert (await session.get(AgentJob, job.id)).status == "completed"
        assert await session.scalar(select(func.count()).select_from(ProjectMessage)) == 2


async def test_queue_timeout_preserves_customer_input_without_model_call(auth_api):
    store, actor, pid, _, run = await scenario(auth_api)
    async with store.database.session() as session:
        row = await session.scalar(select(AgentJob))
        row.queued_until = clock() - timedelta(seconds=1)
    await reconcile_jobs(store.database, None, RecoverySettings(), QueueSettings())
    result = await store.run(actor, pid, UUID(run["id"]))
    assert result["status"] == "failed" and "等待处理" in result["answer"]
    async with store.database.session() as session:
        row = await session.scalar(select(AgentJob))
        assert row.attempts == 0 and row.settled


async def test_worker_runs_existing_graph_once_and_rechecks_revoked_login(auth_api, monkeypatch):
    store, actor, pid, _, run = await scenario(auth_api)
    settings = Settings(_env_file=None, agent=AgentSettings(api_key=SecretStr("test-only")))
    worker = JobWorker(store.database, None, settings)
    calls = []

    async def complete(messages, **kwargs):
        calls.append(messages)
        if kwargs.get("on_text"):
            await kwargs["on_text"]("可以先讲讲当前做法。")
        return {"content": "可以先讲讲当前做法。"}, {"total_tokens": 20}

    monkeypatch.setattr(worker.runtime, "complete", complete)
    try:
        await worker.tick()
        await asyncio.gather(*worker.tasks)
        result = await store.run(actor, pid, UUID(run["id"]))
        assert result["status"] == "completed" and len(calls) == 1
        await worker.tick()
        assert not worker.tasks and len(calls) == 1
        other = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
        denied, _ = await store.start_run(
            actor, UUID(other["id"]), SendMessage(request_id=uuid4(), content="应被撤销阻止")
        )
        from backend.models.orm import LoginSession

        async with store.database.session() as session:
            (await session.get(LoginSession, actor.session_id)).revoked_at = clock()
        await worker.tick()
        await asyncio.gather(*worker.tasks)
        async with store.database.session() as session:
            assert (await session.get(ProjectRun, UUID(denied["id"]))).status == "failed"
        assert len(calls) == 1
    finally:
        await worker.runtime.close()


async def test_same_project_review_waits_and_body_is_pinned(auth_api):
    client, _, database = auth_api
    store, actor, pid, _, run = await scenario(auth_api)
    tokens = await login(client)
    doc = await draft(client, headers(tokens), str(pid))
    reviews = ReviewStore(database)
    review, _ = await reviews.start(
        actor,
        pid,
        UUID(doc["id"]),
        StartReview(request_id=uuid4(), expected_version=doc["version"]),
    )
    jobs = JobStore(database)
    first = await jobs.claim("worker-a", 4)
    assert first.target_id == UUID(run["id"])
    assert await jobs.claim("worker-b", 4) is None
    assert await jobs.finish(first.id, first.token, "failed")
    second = await jobs.claim("worker-b", 4)
    assert second.kind == "review" and second.target_id == UUID(review["id"])
    assert (await reviews.load(actor, pid, UUID(doc["id"]), second.target_id))[
        "source_content"
    ] == doc["content"]


async def test_queued_review_of_changed_document_does_not_call_model(auth_api, monkeypatch):
    client, service, db = auth_api
    tokens = await login(client)
    actor = await service.authenticate(tokens["access_token"])
    p = await project(client, headers(tokens))
    pid = UUID(p["id"])
    doc = await draft(client, headers(tokens), str(pid))
    review, _ = await ReviewStore(db).start(
        actor,
        pid,
        UUID(doc["id"]),
        StartReview(request_id=uuid4(), expected_version=doc["version"]),
    )
    from backend.models.projects import ProjectItem

    async with db.session() as session:
        row = await session.get(ProjectItem, UUID(doc["id"]))
        row.content += "\n客户排队期间补充的新规则。"
        row.version += 1
    settings = Settings(_env_file=None, agent=AgentSettings(api_key=SecretStr("test-only")))
    worker = JobWorker(db, None, settings)

    async def forbidden(*_, **__):
        raise AssertionError("Stale source must not trigger a model request")

    monkeypatch.setattr(worker.runtime, "complete", forbidden)
    try:
        await worker.tick()
        await asyncio.gather(*worker.tasks)
        async with db.session() as session:
            row = await session.get(DocumentReview, UUID(review["id"]))
            assert row.status == "failed" and "等待期间资料已更新" in row.error
            assert (await session.get(ProjectItem, UUID(doc["id"]))).content.endswith("新规则。")
    finally:
        await worker.runtime.close()


@pytest.mark.parametrize("claimed", [False, True])
async def test_queued_or_running_review_blocks_recycling_project(auth_api, claimed):
    client, service, db = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    actor = await service.authenticate(tokens["access_token"])
    p = await project(client, identity)
    pid = UUID(p["id"])
    doc = await draft(client, identity, str(pid))
    await ReviewStore(db).start(
        actor,
        pid,
        UUID(doc["id"]),
        StartReview(request_id=uuid4(), expected_version=doc["version"]),
    )
    if claimed:
        assert await JobStore(db).claim("review-in-progress", 4)
    async with db.session() as session:
        version = (await session.get(Project, pid)).version
    response = await client.post(
        "/api/projects/records/actions",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "action": "trash",
            "targets": [{"id": str(pid), "expected_version": version}],
        },
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROJECT_BUSY"
    async with db.session() as session:
        assert (await session.get(Project, pid)).deleted_at is None


@pytest.mark.skipif(os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Real PG required")
async def test_real_pg_parallel_workers_claim_once_and_share_capacity(postgres_auth):
    service = postgres_auth
    actor = await service.authenticate(
        (await service.login("alice", "Postgres-test-password")).access_token
    )
    db = service.store.database
    store = ConversationStore(db, queue=QueueSettings(per_user=10))
    for _ in range(7):
        p = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
        await store.start_run(
            actor, UUID(p["id"]), SendMessage(request_id=uuid4(), content="并行领取验证")
        )

    jobs = JobStore(db)
    claims = await asyncio.gather(*(jobs.claim(f"worker-{i}", 4) for i in range(12)))
    claimed = [job for job in claims if job]
    assert len(claimed) == 4 and len({job.id for job in claimed}) == 4
    async with db.session() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AgentJob).where(AgentJob.status == "queued")
            )
            == 3
        )


@PG
async def test_real_pg_transaction_crossing_lease_expiry_rolls_back(postgres_auth):
    service = postgres_auth
    actor = await service.authenticate(
        (await service.login("alice", "Postgres-test-password")).access_token
    )
    db = service.store.database
    store = ConversationStore(db)
    p = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
    run, _ = await store.start_run(
        actor, UUID(p["id"]), SendMessage(request_id=uuid4(), content="事务过期验证")
    )
    job = await JobStore(db).claim("slow-transaction", 4)
    async with db.session() as session:
        (await session.get(AgentJob, job.id)).lease_until = clock() + timedelta(seconds=0.2)
    token = job_lease.set(JobLease(job.id, job.token))
    try:
        with pytest.raises(ProjectError, match="旧执行不能继续保存"):
            async with db.session() as session:
                (await session.get(ProjectRun, UUID(run["id"]))).answer = "不得提交"
                await asyncio.sleep(0.3)
    finally:
        job_lease.reset(token)
    async with db.session() as session:
        assert (await session.get(ProjectRun, UUID(run["id"]))).answer == ""


async def test_worker_liveness_and_graceful_stop_leave_waiting_jobs_durable(auth_api, monkeypatch):
    store, actor, pid, _, run = await scenario(auth_api)
    next_project = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
    second, _ = await store.start_run(
        actor, UUID(next_project["id"]), SendMessage(request_id=uuid4(), content="第二份等待任务")
    )
    settings = Settings(
        _env_file=None,
        agent=AgentSettings(api_key=SecretStr("test-only"), capacity=1),
        queue=QueueSettings(poll_seconds=0.01, shutdown_seconds=1),
    )
    worker = JobWorker(store.database, None, settings)
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(messages, **kwargs):
        entered.set()
        await release.wait()
        return {"content": "正常退出前完成回答。"}, {}

    monkeypatch.setattr(worker.runtime, "complete", complete)
    consumer = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await JobStore(store.database).available()
        worker.stop()
        release.set()
        await asyncio.wait_for(consumer, 3)
        assert not await JobStore(store.database).available()
        assert (await store.run(actor, pid, UUID(run["id"])))["status"] == "completed"
        async with store.database.session() as session:
            row = await session.scalar(
                select(AgentJob).where(AgentJob.target_id == UUID(second["id"]))
            )
            assert row.status == "queued" and row.attempts == 0
    finally:
        release.set()
        worker.stop()
        await consumer
        await worker.runtime.close()


PROCESS_WORKER = r"""
import asyncio, os
from pydantic import SecretStr
from backend.config import Settings, AgentSettings, get_settings
from backend.db.session import Database, create_database
from backend.services.job_worker import JobWorker

async def main():
    base = create_database(get_settings().database)
    db = Database(base.engine.execution_options(
        schema_translate_map={None: os.environ["SF_TEST_JOB_SCHEMA"]}))
    config = Settings(_env_file=None,
        agent=AgentSettings(api_key=SecretStr("test-only"), capacity=1))
    worker = JobWorker(db, None, config)
    async def complete(messages, **kwargs):
        print("MODEL_ENTERED", flush=True)
        if os.environ["SF_TEST_WORKER_MODE"] == "block":
            await asyncio.Event().wait()
        return {"content": "独立进程保存的回答。"}, {}
    worker.runtime.complete = complete
    try:
        await worker.tick()
        await asyncio.gather(*worker.tasks)
    finally:
        await worker.runtime.close()
        await base.close()
asyncio.run(main())
"""


@PG
async def test_real_pg_killed_process_is_fenced_and_new_process_runs_waiting_job(
    postgres_auth, postgres
):
    service = postgres_auth
    actor = await service.authenticate(
        (await service.login("alice", "Postgres-test-password")).access_token
    )
    db = service.store.database
    store = ConversationStore(db)
    targets = []
    for _ in range(2):
        p = await store.start_consultation(actor, StartConsultation(request_id=uuid4()))
        run, _ = await store.start_run(
            actor, UUID(p["id"]), SendMessage(request_id=uuid4(), content="独立进程中断验证")
        )
        targets.append(UUID(run["id"]))
    env = dict(os.environ, SF_TEST_JOB_SCHEMA=postgres[1], SF_TEST_WORKER_MODE="block")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PROCESS_WORKER,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (await asyncio.wait_for(child.stdout.readline(), 15)).strip() == b"MODEL_ENTERED"
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
    async with db.session() as session:
        first = await session.scalar(select(AgentJob).where(AgentJob.target_id == targets[0]))
        assert first.status == "running" and first.attempts == 1
        # Only this isolated fixture's lease is advanced; the next process performs real recovery.
        first.lease_until = clock() - timedelta(seconds=1)
    env["SF_TEST_WORKER_MODE"] = "finish"
    replacement = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PROCESS_WORKER,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(replacement.communicate(), 20)
    assert replacement.returncode == 0, err.decode(errors="replace")
    assert out.count(b"MODEL_ENTERED") == 1
    async with db.session() as session:
        first = await session.scalar(select(AgentJob).where(AgentJob.target_id == targets[0]))
        second = await session.scalar(select(AgentJob).where(AgentJob.target_id == targets[1]))
        assert first.status == "failed" and first.settled and first.attempts == 1
        assert second.status == "completed" and second.attempts == 1
        assert (await session.get(ProjectRun, targets[0])).status == "failed"
        assert (await session.get(ProjectRun, targets[1])).answer == "独立进程保存的回答。"

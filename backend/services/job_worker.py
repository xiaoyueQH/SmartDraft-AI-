"""Independent bounded consumers of durable Agent work, sharing the existing graphs."""

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from uuid import uuid4

from sqlalchemy import select

from backend.agents.runtime import AgentRuntime
from backend.config import get_settings
from backend.core.job_context import JobLease, job_lease
from backend.db.auth_store import AuthStore
from backend.db.conversation_store import ConversationStore
from backend.db.job_store import JobStore
from backend.db.review_store import ReviewStore
from backend.models.auth import AuthError
from backend.models.projects import ProjectError, ProjectRun
from backend.models.review import DocumentReview

logger = logging.getLogger("supportflow.worker")


async def reconcile_jobs(database, cache, recovery, queue):
    jobs = JobStore(database, queue)
    await jobs.expire()
    chat = ConversationStore(database, cache, recovery, queue)
    review = ReviewStore(database, cache, queue)
    for job in await jobs.unsettled():
        # Idempotent settlement also repairs a crash after fencing but before business cleanup.
        if job.kind == "run":
            await chat.fail(job.target_id, job.error)
        else:
            await review.fail_review(job.target_id, job.error)
        async with database.session() as session:
            target = await session.get(
                ProjectRun if job.kind == "run" else DocumentReview, job.target_id
            )
            success = target and target.status in {"completed", "ready", "applied", "discarded"}
        await jobs.settle(job.id, "completed" if success else "failed")
        await chat.settle(job.project_id)


class JobWorker:
    def __init__(self, database, cache, settings=None, *, runtime=None):
        self.settings = settings or get_settings()
        self.database, self.cache = database, cache
        self.runtime = runtime or AgentRuntime(self.settings.agent, recovery=self.settings.recovery)
        self.runtime.execution_database = database
        self.jobs = JobStore(database, self.settings.queue)
        self.worker_id = uuid4().hex
        self.tasks = set()
        self.stopping = asyncio.Event()
        self.last_heartbeat = 0.0

    async def beat(self, job, task):
        try:
            while True:
                await asyncio.sleep(self.settings.queue.heartbeat_seconds)
                if not await self.jobs.heartbeat(job.id, job.token):
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Cannot prove ownership while storage is unavailable: stop all further effects.
            logger.warning("worker_heartbeat_failed error_type=%s", type(error).__name__)
            task.cancel()

    async def execute(self, job):
        current = asyncio.current_task()
        heartbeat = asyncio.create_task(self.beat(job, current))
        context = job_lease.set(JobLease(job.id, job.token))
        error = ""
        reserved = False
        try:
            principal = await AuthStore(self.database).authenticate(job.actor_id, job.session_id)
            chat = ConversationStore(
                self.database, self.cache, self.settings.recovery, self.settings.queue
            )
            await chat.authorize(principal, job.project_id)
            self.runtime.reserve()
            reserved = True
            if job.kind == "review":
                review = ReviewStore(self.database, self.cache, self.settings.queue)
                await review.validate_execution(
                    principal, job.project_id, job.item_id, job.target_id
                )
                await review.event(job.target_id, "正在检查文档", type="queue")
                reserved = False  # execute_* owns release, including cancellation.
                await self.runtime.execute_review(
                    review, principal, job.project_id, job.item_id, job.target_id
                )
            else:
                async with self.database.session() as session:
                    run = await session.scalar(
                        select(ProjectRun).where(
                            ProjectRun.id == job.target_id,
                            ProjectRun.project_id == job.project_id,
                            ProjectRun.actor_id == job.actor_id,
                        )
                    )
                    if not run or run.status != "running":
                        return
                if run.options.get("silent"):
                    facts = await chat.load_facts(principal, job.project_id, run)
                    reserved = False
                    await self.runtime.execute_facts(chat, principal, job.project_id, facts)
                else:
                    await chat.event(
                        job.target_id, {"type": "queue", "summary": "正在处理您的请求"}
                    )
                    reserved = False
                    await self.runtime.execute(
                        chat, principal, job.project_id, job.target_id, run.prompt
                    )
        except asyncio.CancelledError:
            error = "本次处理已中断，已保存资料保留，请查看结果后重试。"
        except Exception as exc:
            logger.warning(
                "worker_execution_failed job_id=%s error_type=%s", job.id, type(exc).__name__
            )
            error = (
                exc.message
                if isinstance(exc, (ProjectError, AuthError))
                else "本次请求未完成，请确认登录和项目状态后重试；已有资料保留。"
            )
        finally:
            if reserved:
                self.runtime.release()
            job_lease.reset(context)
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            try:
                # Only the live owner can finish. Lost leases are reconciled by the sweeper.
                if await self.jobs.heartbeat(job.id, job.token):
                    guard = job_lease.set(JobLease(job.id, job.token))
                    try:
                        if job.kind == "run":
                            await ConversationStore(
                                self.database,
                                self.cache,
                                self.settings.recovery,
                                self.settings.queue,
                            ).fail(job.target_id, error or "本次处理暂未完成，已有资料保留。")
                        else:
                            await ReviewStore(
                                self.database, self.cache, self.settings.queue
                            ).fail_review(
                                job.target_id, error or "本次检查暂未完成，原稿保持不变。"
                            )
                        async with self.database.session() as session:
                            target = await session.get(
                                ProjectRun if job.kind == "run" else DocumentReview, job.target_id
                            )
                            success = target and target.status in {
                                "completed",
                                "ready",
                                "applied",
                                "discarded",
                            }
                    finally:
                        job_lease.reset(guard)
                    await self.jobs.finish(
                        job.id, job.token, "completed" if success else "failed", error
                    )
                    await ConversationStore(
                        self.database, self.cache, self.settings.recovery, self.settings.queue
                    ).settle(job.project_id)
            except Exception as exc:
                logger.warning(
                    "worker_settlement_deferred job_id=%s error_type=%s", job.id, type(exc).__name__
                )

    async def tick(self):
        if monotonic() - self.last_heartbeat >= self.settings.queue.heartbeat_seconds:
            await self.jobs.worker_heartbeat(self.worker_id, self.runtime.configured)
            self.last_heartbeat = monotonic()
        await reconcile_jobs(self.database, self.cache, self.settings.recovery, self.settings.queue)
        if not self.runtime.configured or self.stopping.is_set():
            return
        while len(self.tasks) < self.settings.agent.capacity:
            job = await self.jobs.claim(self.worker_id, self.settings.agent.capacity)
            if not job:
                break
            task = asyncio.create_task(self.execute(job))
            self.tasks.add(task)
            self.runtime.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(self.runtime.tasks.discard)

    async def run(self):
        try:
            while not self.stopping.is_set():
                try:
                    await self.tick()
                except Exception as error:
                    logger.warning("worker_poll_deferred error_type=%s", type(error).__name__)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), self.settings.queue.poll_seconds)
        finally:
            self.stopping.set()
            with suppress(Exception):
                await self.jobs.worker_heartbeat(self.worker_id, False)
            if self.tasks:
                _, pending = await asyncio.wait(
                    tuple(self.tasks), timeout=self.settings.queue.shutdown_seconds
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    def stop(self):
        self.stopping.set()

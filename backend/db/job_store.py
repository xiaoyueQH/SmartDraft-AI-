"""Short transactional admission/claims. No model request runs inside these locks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, text

from backend.config import QueueSettings
from backend.models.jobs import AgentJob, AgentWorker
from backend.models.projects import ProjectError


def clock():
    return datetime.now(UTC)


async def database_clock(session):
    if session.get_bind().dialect.name == "postgresql":
        return await session.scalar(select(func.clock_timestamp()))
    return clock()


async def gate(session):
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '1500ms'"))
        # Admission and claim serialize only their small metadata transactions.
        await session.execute(text("SELECT pg_advisory_xact_lock(73160420261030)"))


async def enqueue(
    session, principal, project_id, target_id, *, kind="run", item_id=None, settings=None
):
    settings = settings or QueueSettings()
    await gate(session)
    existing = await session.scalar(
        select(AgentJob).where(AgentJob.kind == kind, AgentJob.target_id == target_id)
    )
    if existing:
        return existing
    active = AgentJob.status.in_(["queued", "running"])
    count = await session.scalar(select(func.count()).select_from(AgentJob).where(active))
    own = await session.scalar(
        select(func.count())
        .select_from(AgentJob)
        .where(active, AgentJob.actor_id == principal.account.id)
    )
    if count >= settings.capacity or own >= settings.per_user:
        raise ProjectError("QUEUE_FULL", "等待处理的请求较多，请稍后再试；本次未重复受理。", 503)
    now = await database_clock(session)
    job = AgentJob(
        id=uuid4(),
        kind=kind,
        target_id=target_id,
        project_id=project_id,
        actor_id=principal.account.id,
        session_id=principal.session_id,
        item_id=item_id,
        status="queued",
        created_at=now,
        queued_until=now + timedelta(seconds=settings.wait_seconds),
    )
    session.add(job)
    await session.flush()
    return job


async def protected(session, kind, target_id):
    """The queue, rather than legacy created_at+180s recovery, owns these executions."""
    return bool(
        await session.scalar(
            select(AgentJob.id)
            .where(
                AgentJob.kind == kind,
                AgentJob.target_id == target_id,
                AgentJob.status.in_(["queued", "running"]),
            )
            .limit(1)
        )
    )


class JobStore:
    def __init__(self, database, settings=None):
        self.database, self.settings = database, settings or QueueSettings()

    async def claim(self, worker_id, capacity):
        async with self.database.session() as session:
            await gate(session)
            now = await database_clock(session)
            running = list(
                await session.scalars(
                    select(AgentJob.project_id).where(AgentJob.status == "running")
                )
            )
            if len(running) >= capacity:
                return None
            statement = select(AgentJob).where(
                AgentJob.status == "queued", AgentJob.queued_until > now
            )
            if running:
                statement = statement.where(AgentJob.project_id.not_in(running))
            row = await session.scalar(
                statement.order_by(AgentJob.created_at, AgentJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if not row:
                return None
            row.status, row.token, row.worker_id = "running", uuid4(), worker_id
            row.started_at = now
            row.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
            row.attempts += 1
            await session.flush()
            return row

    async def heartbeat(self, job_id, token):
        async with self.database.session() as session:
            row = await session.scalar(
                select(AgentJob).where(AgentJob.id == job_id).with_for_update()
            )
            now = await database_clock(session)
            if (
                not row
                or row.status != "running"
                or row.token != token
                or (
                    row.lease_until.replace(tzinfo=UTC)
                    if row.lease_until.tzinfo is None
                    else row.lease_until
                )
                <= now
            ):
                return False
            row.lease_until = now + timedelta(seconds=self.settings.lease_seconds)
            return True

    async def finish(self, job_id, token, status, error=""):
        async with self.database.session() as session:
            row = await session.scalar(
                select(AgentJob).where(AgentJob.id == job_id).with_for_update()
            )
            now = await database_clock(session)
            if (
                not row
                or row.status != "running"
                or row.token != token
                or (
                    row.lease_until.replace(tzinfo=UTC)
                    if row.lease_until.tzinfo is None
                    else row.lease_until
                )
                <= now
            ):
                return False
            row.status, row.error, row.finished_at = status, error[:300], now
            row.settled = True
            return True

    async def expire(self):
        """Fence first, then reconcile the business receipt in separate transactions."""
        async with self.database.session() as session:
            now = await database_clock(session)
            rows = list(
                await session.scalars(
                    select(AgentJob)
                    .where(
                        or_(
                            (AgentJob.status == "queued") & (AgentJob.queued_until <= now),
                            (AgentJob.status == "running") & (AgentJob.lease_until <= now),
                        )
                    )
                    .order_by(AgentJob.created_at)
                    .limit(40)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.error = (
                    "等待处理时间较长，本次请求已结束，请稍后重试。"
                    if row.status == "queued"
                    else "本次处理已中断，已保存资料保留；请查看结果后重试未完成部分。"
                )
                row.status, row.finished_at = "failed", now
                row.token = None
            await session.flush()

    async def unsettled(self):
        async with self.database.session() as session:
            return list(
                await session.scalars(
                    select(AgentJob)
                    .where(AgentJob.status == "failed", AgentJob.settled.is_(False))
                    .order_by(AgentJob.finished_at)
                    .limit(40)
                )
            )

    async def settle(self, job_id, status):
        async with self.database.session() as session:
            row = await session.scalar(
                select(AgentJob).where(AgentJob.id == job_id).with_for_update()
            )
            if row and row.status == "failed" and not row.settled:
                row.status, row.settled = status, True

    async def worker_heartbeat(self, worker_id, accepting=True):
        async with self.database.session() as session:
            now = await database_clock(session)
            row = await session.get(AgentWorker, worker_id)
            if row is None:
                session.add(AgentWorker(id=worker_id, heartbeat_at=now, accepting=accepting))
            else:
                row.heartbeat_at, row.accepting = now, accepting
            await session.execute(
                delete(AgentWorker).where(
                    AgentWorker.heartbeat_at < now - timedelta(days=1),
                    AgentWorker.id != worker_id,
                )
            )

    async def available(self):
        async with self.database.session() as session:
            now = await database_clock(session)
            return bool(
                await session.scalar(
                    select(AgentWorker.id)
                    .where(
                        AgentWorker.accepting.is_(True),
                        AgentWorker.heartbeat_at
                        > now - timedelta(seconds=self.settings.lease_seconds),
                    )
                    .limit(1)
                )
            )

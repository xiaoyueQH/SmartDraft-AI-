from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.config import DatabaseSettings
from backend.core.job_context import job_lease


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory.begin() as session:
            lease = job_lease.get()
            if lease is not None:
                await self._check_lease(session, lease)
            yield session
            if lease is not None:
                # The body may wait for a business row lock. An expired owner must
                # roll back even if it entered the transaction before expiry.
                await self._check_lease(session, lease)

    @staticmethod
    async def _check_lease(session, lease):
        from datetime import UTC, datetime

        from sqlalchemy import func

        from backend.models.jobs import AgentJob
        from backend.models.projects import ProjectError

        statement = (
            select(AgentJob.status, AgentJob.token, AgentJob.lease_until)
            .where(AgentJob.id == lease.job_id)
            .with_for_update(read=True)
        )
        row = (await session.execute(statement)).one_or_none()
        expires = (
            row.lease_until.replace(tzinfo=UTC)
            if row and row.lease_until and row.lease_until.tzinfo is None
            else (row.lease_until if row else None)
        )
        checked_at = (
            await session.scalar(select(func.clock_timestamp()))
            if session.get_bind().dialect.name == "postgresql"
            else datetime.now(UTC)
        )
        if (
            not row
            or row.status != "running"
            or row.token != lease.token
            or not expires
            or expires <= checked_at
        ):
            raise ProjectError(
                "TASK_LEASE_LOST", "本次处理已交由系统收尾，旧执行不能继续保存。", 409
            )

    async def ping(self) -> None:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()


def create_database(settings: DatabaseSettings) -> Database:
    engine = create_async_engine(
        settings.connection_url(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=1800,
        hide_parameters=True,
        connect_args={
            "timeout": settings.connect_timeout_seconds,
            "command_timeout": settings.query_timeout_seconds,
            "server_settings": {
                "application_name": "supportflow",
                "statement_timeout": str(int(settings.query_timeout_seconds * 1000)),
                "idle_in_transaction_session_timeout": "10000",
            },
        },
    )
    return Database(engine)

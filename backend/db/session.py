from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.config import DatabaseSettings


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory.begin() as session:
            yield session

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

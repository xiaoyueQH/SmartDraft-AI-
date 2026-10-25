import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings
from backend.models import projects  # noqa: F401
from backend.models.orm import Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # Historical/external tables are retained unless an explicit migration removes them.
    return not (type_ == "table" and reflected and compare_to is None)


def run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    settings = get_settings()
    engine = create_async_engine(
        settings.database.connection_url(),
        poolclass=pool.NullPool,
        connect_args={"timeout": settings.database.connect_timeout_seconds, "command_timeout": 60},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database.connection_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())

"""Start the independent Agent worker from any working directory."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.db.cache import ReadCache  # noqa: E402
from backend.db.session import create_database  # noqa: E402
from backend.services.job_worker import JobWorker  # noqa: E402


async def run(settings):
    database, cache = create_database(settings.database), ReadCache(settings.cache)
    worker = JobWorker(database, cache, settings)
    loop = asyncio.get_running_loop()

    def stop(*_):
        loop.call_soon_threadsafe(worker.stop)

    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, stop)
    try:
        await database.ping()
        await worker.jobs.available()  # Fail fast if the queue migration has not been applied.
        await worker.run()
    finally:
        await worker.runtime.close()
        await cache.close()
        await database.close()


def main():
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        raise SystemExit(f"配置校验失败，请检查字段：{fields}") from None
    logging.basicConfig(
        level=settings.app.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()

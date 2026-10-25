import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.config import CacheSettings
from backend.models.projects import ProjectError

logger = logging.getLogger("supportflow.cache")


class ReadCache:
    """Disposable JSON snapshots; authorization and business state remain in PostgreSQL."""

    def __init__(self, settings: CacheSettings):
        self.settings = settings
        self.client = (
            Redis(
                host=settings.host,
                port=settings.port,
                db=settings.database,
                password=settings.password.get_secret_value() or None,
                max_connections=settings.max_connections,
                socket_connect_timeout=settings.timeout_seconds,
                socket_timeout=settings.timeout_seconds,
                decode_responses=True,
            )
            if settings.enabled
            else None
        )
        self.retry_at = 0
        self.loads = asyncio.Semaphore(settings.load_capacity)
        self.locks = {}
        self.counters = dict.fromkeys(
            ("hits", "misses", "fills", "errors", "bypasses", "invalidations"), 0
        )

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def _redis(self, method, *args, **kwargs):
        if self.client is None or monotonic() < self.retry_at:
            return None
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                return await getattr(self.client, method)(*args, **kwargs)
        except (RedisError, OSError, TimeoutError, UnicodeError) as error:
            self.counters["errors"] += 1
            self.retry_at = monotonic() + self.settings.cooldown_seconds
            logger.warning("cache_unavailable error_type=%s", type(error).__name__)
            return None

    async def invalidate_summary(self):
        if await self._redis("set", f"{self.settings.prefix}:epoch:summary", uuid4().hex, ex=86400):
            self.counters["invalidations"] += 1

    async def _key(self, scope, identity):
        if self.client is None or monotonic() < self.retry_at:
            return None
        epoch = "run"
        if scope == "summary":
            epoch_key = f"{self.settings.prefix}:epoch:summary"
            epoch = await self._redis("get", epoch_key)
            if epoch is None:
                await self._redis("set", epoch_key, uuid4().hex, nx=True, ex=86400)
                epoch = await self._redis("get", epoch_key)
        if epoch is None or monotonic() < self.retry_at:
            return None
        return f"{self.settings.prefix}:{scope}:{epoch}:{identity}"

    async def _get(self, key):
        raw = await self._redis("get", key) if key else None
        if raw is not None:
            try:
                result = json.loads(raw)
                if isinstance(result, dict):
                    self.counters["hits"] += 1
                    return result
            except (ValueError, TypeError):
                pass
        return None

    @asynccontextmanager
    async def _single_flight(self, identity):
        if identity not in self.locks:
            if len(self.locks) >= 1024:
                raise ProjectError("READ_BUSY", "读取繁忙，请稍后重试", 503)
            self.locks[identity] = [asyncio.Lock(), 0]
        entry = self.locks[identity]
        entry[1] += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.settings.load_wait_seconds):
                    await entry[0].acquire()
                    acquired = True
            except TimeoutError:
                raise ProjectError("READ_BUSY", "读取繁忙，请稍后重试", 503) from None
            yield
        finally:
            if acquired:
                entry[0].release()
            entry[1] -= 1
            if not entry[1]:
                del self.locks[identity]

    async def get_or_load(self, scope, identity, loader, ttl):
        key = await self._key(scope, identity)
        cached = await self._get(key)
        if cached is not None:
            return cached
        self.counters["misses"] += 1
        async with self._single_flight((scope, identity)):
            cached = await self._get(key)
            if cached is not None:
                return cached
            acquired = False
            try:
                try:
                    async with asyncio.timeout(self.settings.load_wait_seconds):
                        await self.loads.acquire()
                        acquired = True
                except TimeoutError:
                    raise ProjectError("READ_BUSY", "读取繁忙，请稍后重试", 503) from None
                value = await loader()
            finally:
                if acquired:
                    self.loads.release()
            if key:
                payload = json.dumps(value, ensure_ascii=False)
                if len(payload.encode("utf-8")) <= 262144:
                    saved = await self._redis(
                        "set", key, payload, px=int(ttl * random.uniform(0.8, 1) * 1000)
                    )
                    if saved:
                        self.counters["fills"] += 1
            else:
                self.counters["bypasses"] += 1
            return value

    async def status(self):
        available = bool(await self._redis("ping"))
        return {
            "enabled": self.settings.enabled,
            "available": available,
            "circuit_open": monotonic() < self.retry_at,
            "counters": dict(self.counters),
        }

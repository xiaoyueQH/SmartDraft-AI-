import asyncio

import httpx

from backend.core.llm.client import build_model, complete
from backend.core.llm.models import ModelError, ModelPurpose


class ModelFactory:
    """One lifecycle-owned transport; aliases sharing a model share its concurrency limit."""

    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=settings.capacity, max_keepalive_connections=settings.capacity
            ),
        )
        # The SDK initializes both transports; inference only uses the async client.
        self.sync_http = httpx.Client(trust_env=False)
        self.profiles = {
            purpose: getattr(settings, purpose).model_copy(
                update={
                    "model": getattr(settings, purpose).model or settings.model,
                }
            )
            for purpose in ("consultation", "draft_planner", "draft_writer")
        }
        capacities = {}
        for profile in self.profiles.values():
            capacities[profile.model] = min(capacities.get(profile.model, 16), profile.concurrency)
        self.slots = {model: asyncio.Semaphore(capacity) for model, capacity in capacities.items()}
        self.adapters = {}

    async def complete(self, messages, *, purpose: ModelPurpose = "consultation", **kwargs):
        profile = self.profiles[purpose]
        slot = self.slots[profile.model]
        try:
            async with asyncio.timeout(profile.wait_seconds):
                await slot.acquire()
        except TimeoutError as error:
            raise ModelError("MODEL_BUSY", "当前模型服务繁忙，请稍后重试") from error
        try:
            if purpose not in self.adapters:
                self.adapters[purpose] = build_model(
                    self.http, self.sync_http, self.settings, profile
                )
            return await complete(
                self.adapters[purpose],
                profile=profile,
                purpose=purpose,
                messages=messages,
                **kwargs,
            )
        finally:
            slot.release()

    async def close(self):
        self.adapters.clear()
        self.sync_http.close()
        await self.http.aclose()

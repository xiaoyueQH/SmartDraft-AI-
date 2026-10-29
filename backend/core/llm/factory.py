import asyncio
from time import monotonic

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
            for purpose in ("consultation", "draft_planner", "draft_writer", "draft_reviewer")
        }
        capacities = {}
        self.fallbacks = {
            purpose: profile
            for purpose, profile in settings.fallbacks.items()
            if profile.model and profile.model != self.profiles[purpose].model
        }
        for profile in [*self.profiles.values(), *self.fallbacks.values()]:
            capacities[profile.model] = min(capacities.get(profile.model, 16), profile.concurrency)
        self.slots = {model: asyncio.Semaphore(capacity) for model, capacity in capacities.items()}
        self.adapters = {}

    async def complete(
        self, messages, *, purpose: ModelPurpose = "consultation", max_attempts=1, **kwargs
    ):
        primary = self.profiles[purpose]
        backup = self.fallbacks.get(purpose) if max_attempts >= 2 else None
        deadline = monotonic() + kwargs.get("timeout_seconds", self.settings.timeout_seconds)
        emitted = False
        callback = kwargs.get("on_text")

        async def write(text):
            nonlocal emitted
            emitted = emitted or bool(text)
            await callback(text)

        if callback:
            kwargs["on_text"] = write
        profiles = [primary, backup] if backup else [primary]
        errors = []
        for index, profile in enumerate(profiles):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ModelError("MODEL_TIMEOUT", "本轮处理超时，已保存内容保留")
            try:
                async with asyncio.timeout(remaining):
                    message, usage = await self._complete(
                        messages,
                        profile=profile,
                        purpose=purpose,
                        **{**kwargs, "timeout_seconds": remaining},
                    )
                return message, {
                    **usage,
                    "attempts": index + 1,
                    **({"fallback_from": primary.model, "attempt_errors": errors} if index else {}),
                }
            except TimeoutError:
                raise ModelError("MODEL_TIMEOUT", "本轮处理超时，已保存内容保留") from None
            except ModelError as error:
                errors.append(error.code)
                if (
                    emitted
                    or index == len(profiles) - 1
                    or error.code not in {"MODEL_BUSY", "MODEL_TIMEOUT", "MODEL_RESPONSE_FAILED"}
                ):
                    raise

    async def _complete(self, messages, *, profile, purpose, **kwargs):
        slot = self.slots[profile.model]
        try:
            async with asyncio.timeout(profile.wait_seconds):
                await slot.acquire()
        except TimeoutError as error:
            raise ModelError("MODEL_BUSY", "当前模型服务繁忙，请稍后重试") from error
        try:
            key = purpose if profile is self.profiles[purpose] else f"fallback:{purpose}"
            if key not in self.adapters:
                self.adapters[key] = build_model(self.http, self.sync_http, self.settings, profile)
            return await complete(
                self.adapters[key],
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

import asyncio
import json
import logging
from time import monotonic

from backend.agents.common import tool_json
from backend.agents.consultation.graph import ConsultationWorkflow
from backend.agents.project.graph import ProjectWorkflow
from backend.config import AgentSettings
from backend.core.llm.factory import ModelFactory
from backend.core.llm.models import ModelError, ModelPurpose
from backend.models.projects import ProjectError

logger = logging.getLogger("supportflow.agent")


class AgentRuntime:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.models = ModelFactory(settings)
        self.client = self.models.http
        self.tasks: set[asyncio.Task] = set()
        self.active = 0
        self.previews = {}
        self.stream_viewers = {}

    async def preview(self, store, run_id):
        key = str(run_id)
        local = self.previews.get(key)
        if local:
            return local
        if store.cache:
            raw = await store.cache._redis("get", f"{store.cache.settings.prefix}:live:{key}")
            if raw:
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict) and isinstance(value.get("text"), str):
                        return value
                except (ValueError, TypeError):
                    pass
        return None

    async def stream_writer(self, store, run_id, *, kind="reply", title=""):
        key, text, sent = str(run_id), "", 0.0

        async def write(delta):
            nonlocal text, sent
            text = (text + delta)[:40000]
            visible = text
            if kind == "reply" and text.lstrip().startswith(("{", "[", "```")):
                visible = ""
            snapshot = {"text": visible, "kind": kind, "title": title}
            self.previews[key] = snapshot
            if monotonic() - sent < 0.12 and delta:
                return
            sent = monotonic()
            if store.cache:
                await store.cache._redis(
                    "set",
                    f"{store.cache.settings.prefix}:live:{key}",
                    json.dumps(snapshot, ensure_ascii=False),
                    ex=180,
                )

        await write("")
        return write

    @property
    def configured(self):
        return bool(self.settings.api_key.get_secret_value())

    def reserve(self):
        if not self.configured:
            raise ProjectError(
                "AGENT_NOT_CONFIGURED",
                "业务顾问暂时不可用，请联系负责人。您仍可手动整理需求和工单。",
                503,
            )
        if self.active >= self.settings.capacity:
            raise ProjectError("AGENT_BUSY", "当前服务繁忙，请稍后重试", 503)
        self.active += 1

    def release(self):
        self.active -= 1

    def launch(self, store, principal, project_id, run_id, prompt):
        task = asyncio.create_task(self.execute(store, principal, project_id, run_id, prompt))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.models.close()

    async def execute(self, store, principal, project_id, run_id, prompt):
        started = monotonic()
        try:
            async with asyncio.timeout(
                max(self.settings.run_seconds, self.settings.draft_run_seconds)
            ) as deadline:
                snapshot = await store.detail(principal, project_id)
                if snapshot["project"]["phase"] != "discovery":
                    deadline.reschedule(
                        asyncio.get_running_loop().time() + self.settings.run_seconds
                    )
                if snapshot["project"]["phase"] == "discovery":
                    workflow = ConsultationWorkflow(
                        self, store, principal, project_id, run_id, snapshot
                    )
                    workflow.execution_deadline = deadline.when()
                    await workflow.run(prompt)
                    return
                await ProjectWorkflow(self, store, principal, project_id, run_id, snapshot).run(
                    prompt
                )
        except asyncio.CancelledError:
            await store.fail(run_id, "服务正在停止，本轮未提交草稿，请重新发送。")
            raise
        except Exception as exc:
            logger.warning(
                "agent_run_failed run_id=%s error_type=%s code=%s",
                run_id,
                type(exc).__name__,
                exc.code if isinstance(exc, ProjectError) else "EXECUTION_ERROR",
            )
            message = (
                exc.message
                if isinstance(exc, ProjectError)
                else "这次处理暂未完成，已有资料已保留。请稍后重试，或手动整理需求。"
            )
            await store.fail(run_id, message)
        finally:
            self.previews.pop(str(run_id), None)
            if store.cache:
                await store.cache._redis("delete", f"{store.cache.settings.prefix}:live:{run_id}")
            self.release()
            logger.info(
                "agent_run_finished run_id=%s duration_ms=%s",
                run_id,
                round((monotonic() - started) * 1000),
            )

    async def complete(
        self,
        messages,
        *,
        allow_tools,
        max_output_tokens=4000,
        tools=None,
        json_output=False,
        timeout_seconds=None,
        purpose: ModelPurpose = "consultation",
        on_text=None,
    ):
        if len(tool_json(messages)) > 100000:
            raise ProjectError("CONTEXT_BUDGET", "资料超出本轮预算，请按模块分别整理", 503)
        try:
            return await self.models.complete(
                messages,
                purpose=purpose,
                tools=tools if allow_tools else None,
                max_output_tokens=max_output_tokens,
                json_output=json_output,
                timeout_seconds=timeout_seconds or self.settings.timeout_seconds,
                on_text=on_text,
            )
        except ModelError as error:
            raise ProjectError(error.code, error.message, 503) from error

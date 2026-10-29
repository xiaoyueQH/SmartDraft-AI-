import asyncio
import json
import logging
from time import monotonic
from uuid import UUID

from backend.agents.common import tool_json
from backend.agents.consultation.graph import ConsultationWorkflow, run_fact_graph
from backend.agents.project.graph import ProjectWorkflow
from backend.agents.review.graph import run_review
from backend.config import AgentSettings, RecoverySettings
from backend.core.llm.factory import ModelFactory
from backend.core.llm.models import ModelError, ModelPurpose
from backend.db.checkpoints import DatabaseCheckpointer
from backend.db.conversation_store import ConversationStore
from backend.models.projects import ProjectError

logger = logging.getLogger("supportflow.agent")


class AgentRuntime:
    def __init__(self, settings: AgentSettings, *, recovery: RecoverySettings | None = None):
        self.settings = settings
        self.recovery = recovery or RecoverySettings()
        self.models = ModelFactory(settings)
        self.client = self.models.http
        self.tasks: set[asyncio.Task] = set()
        self.active = 0
        self.previews = {}
        self.stream_viewers = {}
        self.fact_claims = set()
        self.closing = False

    async def try_facts(self, store, principal, project_id):
        if self.closing:
            return
        if (
            project_id in self.fact_claims
            or not self.configured
            or self.active >= self.settings.capacity
        ):
            return
        self.fact_claims.add(project_id)
        reserved = False
        try:
            self.reserve()
            reserved = True
            chat = ConversationStore(
                store.database, store.cache, getattr(store, "recovery", self.recovery)
            )
            job = await chat.claim_facts(principal, project_id)
            if not job:
                return
            task = asyncio.create_task(self.execute_facts(chat, principal, project_id, job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            reserved = False
        except Exception as error:
            logger.warning(
                "fact_claim_deferred project_id=%s error_type=%s", project_id, type(error).__name__
            )
        finally:
            if reserved:
                self.release()
            self.fact_claims.discard(project_id)

    async def execute_facts(self, store, principal, project_id, job):
        try:
            async with asyncio.timeout(self.settings.run_seconds):
                await run_fact_graph(self, store, principal, project_id, job)
        except asyncio.CancelledError:
            await store.fail_facts(project_id, UUID(job["run_id"]))
            raise
        except Exception as error:
            logger.warning(
                "fact_extraction_deferred project_id=%s error_type=%s",
                project_id,
                type(error).__name__,
            )
            await store.fail_facts(project_id, UUID(job["run_id"]))
        finally:
            self.release()
            await store.settle(project_id)
            await self.try_facts(store, principal, project_id)

    async def settle_conversation(self, store, project_id):
        try:
            await ConversationStore(
                store.database, store.cache, getattr(store, "recovery", self.recovery)
            ).settle(project_id)
        except Exception as error:
            logger.warning(
                "handover_settle_deferred project_id=%s error_type=%s",
                project_id,
                type(error).__name__,
            )

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

    async def stream_writer(self, store, run_id, *, kind="reply", title="", transform=None):
        key, text, sent = str(run_id), "", 0.0

        async def write(delta):
            nonlocal text, sent
            text = (text + delta)[:40000]
            visible = text
            if transform:
                visible = transform(visible)
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
        if self.closing:
            raise ProjectError("AGENT_STOPPING", "服务正在更新，请稍后继续", 503)
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

    def launch_review(self, store, principal, project_id, item_id, review_id):
        task = asyncio.create_task(
            self.execute_review(store, principal, project_id, item_id, review_id)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def execute_review(self, store, principal, project_id, item_id, review_id):
        try:
            async with asyncio.timeout(self.settings.review_run_seconds):
                await run_review(self, store, principal, project_id, item_id, review_id)
        except asyncio.CancelledError:
            await store.fail_review(review_id, "服务正在停止，检查中断，原稿保持不变。")
            raise
        except Exception as error:
            logger.warning(
                "review_failed review_id=%s error_type=%s", review_id, type(error).__name__
            )
            await store.fail_review(
                review_id,
                error.message
                if isinstance(error, ProjectError)
                else "本次检查暂未完成，原稿保持不变。",
            )
        finally:
            self.release()
            await self.settle_conversation(store, project_id)
            await self.cleanup_checkpoints(store, project_id)

    async def cleanup_checkpoints(self, store, project_id):
        try:
            await DatabaseCheckpointer(store.database, project_id).cleanup_terminal(
                limit=self.settings.checkpoint_cleanup_limit,
                success_days=self.settings.checkpoint_success_days,
                failure_days=self.settings.checkpoint_failure_days,
            )
        except Exception as error:
            logger.warning(
                "checkpoint_cleanup_failed project_id=%s error_type=%s",
                project_id,
                type(error).__name__,
            )

    async def close(self):
        self.closing = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.tasks.clear()
            self.previews.clear()
            self.stream_viewers.clear()
            await self.models.close()

    async def execute(self, store, principal, project_id, run_id, prompt):
        started = monotonic()
        try:
            async with asyncio.timeout(
                max(self.settings.run_seconds, self.settings.draft_run_seconds)
            ) as deadline:
                snapshot = await store.detail(principal, project_id, summaries=True)
                if snapshot["baseline"]:
                    snapshot["baseline"] = await store.item(
                        principal, project_id, UUID(snapshot["baseline"]["id"])
                    )
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
            try:
                if store.cache:
                    await store.cache._redis(
                        "delete", f"{store.cache.settings.prefix}:live:{run_id}"
                    )
            except Exception as error:
                logger.warning(
                    "preview_cleanup_failed run_id=%s error_type=%s", run_id, type(error).__name__
                )
            finally:
                self.release()
            await self.settle_conversation(store, project_id)
            await self.try_facts(store, principal, project_id)
            await self.cleanup_checkpoints(store, project_id)
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
        tool_choice=None,
        max_attempts=1,
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
                tool_choice=tool_choice,
                max_attempts=max_attempts,
            )
        except ModelError as error:
            raise ProjectError(error.code, error.message, 503) from error

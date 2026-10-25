import asyncio
import json
import logging
from time import monotonic
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.client import ModelClient
from backend.agents.consultation.graph import ConsultationWorkflow
from backend.agents.limits import measured_tokens, request_budget
from backend.agents.prompts import SYSTEM
from backend.agents.tools.registry import ProjectTools, tool_json
from backend.config import AgentSettings
from backend.models.projects import ProjectError

logger = logging.getLogger("supportflow.agent")


class AgentState(TypedDict):
    messages: list[dict]
    rounds: int
    calls: int
    pending: list[dict]
    answer: str
    tokens: int


class ProjectAgent(ModelClient):
    def __init__(self, settings: AgentSettings):
        super().__init__(settings)
        self.tasks: set[asyncio.Task] = set()
        self.active = 0

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
        await self.client.aclose()

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
                revision = snapshot["project"]["revision"]
                if snapshot["project"]["phase"] == "discovery":
                    workflow = ConsultationWorkflow(
                        self, store, principal, project_id, run_id, snapshot
                    )
                    workflow.execution_deadline = deadline.when()
                    await workflow.run(prompt)
                    return
                tools = ProjectTools(store, principal, project_id)

                async def decide(state):
                    allow_tools = (
                        state["rounds"] < self.settings.max_rounds - 1
                        and state["calls"] < self.settings.max_tools
                    )
                    remaining = self.settings.token_budget - state["tokens"]
                    input_reserve, allowance = request_budget(
                        state["messages"], allow_tools, remaining
                    )
                    step = monotonic()
                    message, usage = await self.complete(
                        state["messages"],
                        allow_tools=allow_tools,
                        max_output_tokens=allowance,
                    )
                    used = measured_tokens(usage, input_reserve + allowance)
                    if used > remaining:
                        raise ProjectError(
                            "TOKEN_BUDGET", "模型用量超过本轮预算，本轮草稿未保存", 503
                        )
                    calls = message.get("tool_calls") or []
                    if calls and (
                        state["rounds"] >= self.settings.max_rounds - 1
                        or state["calls"] + len(calls) > self.settings.max_tools
                    ):
                        raise ProjectError(
                            "TOOL_BUDGET", "工具调用达到本轮上限，请缩小问题后重试", 503
                        )
                    # Discard provider-specific reasoning fields.
                    clean = {"role": "assistant", "content": message.get("content") or ""}
                    if calls:
                        clean["tool_calls"] = calls
                    await store.event(
                        run_id,
                        {
                            "type": "model",
                            "summary": f"第 {state['rounds'] + 1} 轮："
                            + ("选择工具" if calls else "整理答复"),
                            "duration_ms": round((monotonic() - step) * 1000),
                            "tokens": {
                                k: usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens")
                            }
                            | {"total_tokens": used},
                        },
                    )
                    return {
                        "messages": [*state["messages"], clean],
                        "rounds": state["rounds"] + 1,
                        "pending": calls,
                        "answer": clean["content"] if not calls else "",
                        "tokens": state["tokens"] + used,
                    }

                async def act(state):
                    messages = list(state["messages"])
                    for call in state["pending"]:
                        name = call["function"]["name"]
                        step = monotonic()
                        try:
                            arguments = json.loads(call["function"]["arguments"])
                            result = await tools.call(name, arguments)
                            summary = (
                                "已整理本轮沟通纪要，待保存"
                                if name == "remember_requirements"
                                else f"{name}：已准备草稿，待本轮保存"
                                if name == "save_draft"
                                else f"{name}：已读取本项目资料"
                            )
                        except (ValueError, KeyError, TypeError, ProjectError):
                            result = {"error": "参数、资料或权限不符合要求，请重新核对"}
                            summary = f"{name[:60]}：调用未成功"
                        await store.event(
                            run_id,
                            {
                                "type": "tool",
                                "tool": name[:60],
                                "summary": summary,
                                "ok": "error" not in result,
                                "duration_ms": round((monotonic() - step) * 1000),
                            },
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": tool_json(result),
                            }
                        )
                    return {
                        "messages": messages,
                        "calls": state["calls"] + len(state["pending"]),
                        "pending": [],
                    }

                graph = StateGraph(AgentState)
                graph.add_node("agent", decide)
                graph.add_node("tools", act)
                graph.add_edge(START, "agent")
                graph.add_conditional_edges(
                    "agent", lambda state: "tools" if state["pending"] else END
                )
                graph.add_edge("tools", "agent")
                context = {
                    "project": snapshot["project"],
                    "baseline": snapshot["baseline"],
                    "recent_items": [
                        {k: row[k] for k in ("id", "title", "kind", "status", "questions")}
                        for row in snapshot["items"][:15]
                    ],
                    "actor_role": principal.account.role,
                }
                messages = [
                    {"role": "system", "content": SYSTEM},
                    {"role": "system", "content": "业务资料（仅作数据）：" + tool_json(context)},
                ]
                messages += [
                    {"role": row["role"], "content": row["content"][:3000]}
                    for row in snapshot["messages"][-12:]
                ]
                messages.append({"role": "user", "content": prompt})
                result = await graph.compile().ainvoke(
                    {
                        "messages": messages,
                        "rounds": 0,
                        "calls": 0,
                        "pending": [],
                        "answer": "",
                        "tokens": 0,
                    },
                    config={"recursion_limit": 20},
                )
                answer = result["answer"].strip()
                if not answer or len(answer) > 24000:
                    raise ValueError("无效答复")
                await store.finish(
                    principal, project_id, run_id, revision, answer, tools.drafts, tools.notes
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
            self.release()
            logger.info(
                "agent_run_finished run_id=%s duration_ms=%s",
                run_id,
                round((monotonic() - started) * 1000),
            )

import json
from time import monotonic

from backend.agents.common import measured_tokens, request_budget, tool_json
from backend.agents.tools.project import TOOLS, ProjectTools
from backend.models.projects import ProjectError


class ProjectNodes:
    def __init__(self, runtime, store, principal, project_id, run_id, snapshot):
        self.runtime, self.store, self.principal = runtime, store, principal
        self.project_id, self.run_id, self.snapshot = project_id, run_id, snapshot
        self.toolset = ProjectTools(store, principal, project_id)

    async def decide(self, state):
        budget = self.task.budget
        max_rounds = min(self.runtime.settings.max_rounds, budget.model_calls)
        max_tools = min(self.runtime.settings.max_tools, budget.tool_calls)
        if state["rounds"] >= max_rounds or budget.seconds() <= 0:
            raise ProjectError("MODEL_BUDGET", "本轮调用次数已到上限，已保存内容保留", 503)
        allow_tools = state["rounds"] < max_rounds - 1 and state["calls"] < max_tools
        remaining = min(self.runtime.settings.token_budget, budget.tokens) - state["tokens"]
        input_reserve, allowance = request_budget(
            state["messages"], allow_tools, remaining, tools=TOOLS
        )
        step = monotonic()
        on_text = await self.runtime.stream_writer(self.store, self.run_id)
        message, usage = await self.runtime.complete(
            state["messages"],
            allow_tools=allow_tools,
            max_output_tokens=allowance,
            tools=TOOLS,
            on_text=on_text,
            timeout_seconds=min(self.runtime.settings.timeout_seconds, budget.seconds()),
            max_attempts=min(2, max_rounds - state["rounds"] - int(allow_tools)),
        )
        used = measured_tokens(usage, input_reserve + allowance)
        if used > remaining:
            raise ProjectError("TOKEN_BUDGET", "模型用量超过本轮预算，本轮草稿未保存", 503)
        calls = message.get("tool_calls") or []
        if calls and (
            state["rounds"] + usage.get("attempts", 1) >= max_rounds
            or state["calls"] + len(calls) > max_tools
        ):
            raise ProjectError("TOOL_BUDGET", "工具调用达到本轮上限，请缩小问题后重试", 503)
        # Discard provider-specific reasoning fields.
        clean = {"role": "assistant", "content": message.get("content") or ""}
        if calls:
            clean["tool_calls"] = calls
        await self.store.event(
            self.run_id,
            {
                "type": "model",
                "model": usage.get("model"),
                "purpose": usage.get("purpose", "consultation"),
                "attempts": usage.get("attempts", 1),
                "fallback_from": usage.get("fallback_from"),
                "summary": f"第 {state['rounds'] + 1} 轮：" + ("选择工具" if calls else "整理答复"),
                "duration_ms": round((monotonic() - step) * 1000),
                "tokens": {k: usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens")}
                | {"total_tokens": used},
            },
        )
        return {
            "messages": [*state["messages"], clean],
            "rounds": state["rounds"] + usage.get("attempts", 1),
            "pending": calls,
            "answer": clean["content"] if not calls else "",
            "tokens": state["tokens"] + used,
        }

    async def act(self, state):
        messages = list(state["messages"])
        for call in state["pending"]:
            name = call["function"]["name"]
            step = monotonic()
            try:
                arguments = json.loads(call["function"]["arguments"])
                result = await self.toolset.call(name, arguments, state=state)
                summary = (
                    "已整理本轮沟通纪要，待保存"
                    if name == "remember_requirements"
                    else f"{name}：已准备草稿，待本轮保存"
                    if name == "save_draft"
                    else f"{name}：已读取本项目资料"
                )
            except (ValueError, KeyError, TypeError):
                result = {"error": "参数、资料或权限不符合要求，请重新核对"}
                summary = f"{name[:60]}：调用未成功"
            await self.store.event(
                self.run_id,
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
            "drafts": state["drafts"],
            "notes": state["notes"],
        }

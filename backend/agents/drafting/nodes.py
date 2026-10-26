import asyncio
import time

from backend.agents.common import tool_json
from backend.agents.drafting.prompts import AGENT, CONTINUE
from backend.agents.drafting.state import DraftState
from backend.agents.tools.drafting import RECOVERABLE, DraftTools, error_code, markdown, recoverable
from backend.models.projects import ProjectError


class DraftNodes:
    def __init__(self, flow, state):
        self.flow, self.state = flow, state
        now = asyncio.get_running_loop().time()
        self.deadline = (
            min(
                getattr(flow, "execution_deadline", now + flow.agent.settings.draft_run_seconds),
                now + flow.agent.settings.draft_run_seconds,
            )
            - 12
        )
        self.toolset = DraftTools(self)

    def remaining(self):
        settings = self.flow.agent.settings
        return {
            "model_calls": max(0, settings.draft_max_rounds - self.flow.rounds),
            "tool_calls": max(0, settings.draft_max_tools - self.flow.calls),
            "tokens": max(0, settings.draft_token_budget - self.flow.tokens),
            "finalization_available": not self.state.finalization_attempted,
            "seconds": max(0, round(self.deadline - asyncio.get_running_loop().time(), 1)),
        }

    async def model(self, system, data, *, purpose="draft_planner", **kwargs):
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining < 1:
            raise ProjectError("DRAFT_TIME_BUDGET", "已保留完成内容，其余章节待完善", 503)
        async with asyncio.timeout(
            min(remaining, self.flow.agent.settings.draft_model_timeout_seconds)
        ):
            return await self.flow.model(system, data, purpose=purpose, **kwargs)

    def complete(self, questions=()):
        self.state.questions = list(dict.fromkeys([*self.state.questions, *questions]))[:70]
        missing = self.missing()
        self.state.status = "partial" if missing or self.state.failures else "completed"

    def missing(self):
        if not self.state.plan:
            return []
        return [s for s in self.state.plan.sections if s.id not in self.state.sections]

    def assemble(self):
        if self.state.direct_body:
            return self.state.direct_body
        if not self.state.sections and not self.state.plan:
            return (
                "# 需求资料整理稿\n\n**本次详细说明未生成，"
                "以下保留已提供的信息，尚不能作为完整实施方案。**"
            )
        specs = {s.id: s for s in self.state.plan.sections} if self.state.plan else {}
        specs.update(self.state.section_specs)
        parts = []
        for sid, spec in specs.items():
            body = self.state.sections.get(sid)
            parts.append(
                f"## {spec.title}\n\n"
                + (body or "> 本章暂未生成，请补充细节；已保存的业务资料保留在文末。")
            )
        pending = [
            *self.state.questions,
            *(self.state.plan.pending_questions if self.state.plan else []),
            *(f"“{s.title}”章节尚需补充细节。" for s in self.missing()),
            *(
                f"“{self.state.section_specs[sid].title}”章节尚需补充细节。"
                for sid in self.state.failures
                if sid in self.state.section_specs
            ),
        ]
        parts.append(
            "## 待确认事项\n\n" + ("\n".join("- " + q for q in dict.fromkeys(pending)) or "暂无")
        )
        return "# 需求草案\n\n" + "\n\n".join(parts)

    def restore(self, data):
        restored = DraftState.model_validate(data["draft"])
        for name in DraftState.model_fields:
            setattr(self.state, name, getattr(restored, name))
        for name, used in data["counters"].items():
            setattr(self.flow, name, max(getattr(self.flow, name, 0), used))
        self.deadline = min(
            self.deadline,
            asyncio.get_running_loop().time() + max(0, data["expires_at"] - time.time()),
        )

    def snapshot(self):
        return {
            "draft": self.state.model_dump(mode="json"),
            "counters": {
                name: getattr(self.flow, name, 0) for name in ("rounds", "tokens", "calls")
            },
        }

    def node(self, handler):
        async def invoke(data):
            self.restore(data)
            try:
                update = await handler(data)
            except RECOVERABLE as error:
                if not recoverable(error):
                    raise
                self.state.status = "partial"
                self.state.stop_code = error_code(error)
                await self.flow.event(
                    "draft_agent",
                    "已保留完成内容，其余内容待完善",
                    ok=False,
                    code=self.state.stop_code,
                )
                update = {"pending": []}
            return {**update, **self.snapshot()}

        return invoke

    async def decide(self, data):
        runtime, state = self, self.state
        if runtime.remaining()["model_calls"] <= 0:
            raise ProjectError("MODEL_BUDGET", "本轮次数已用完", 503)
        await self.flow.event("draft_decide", "起草助手判断下一步需要处理的内容")
        message = await runtime.model(
            CONTINUE if state.plan else AGENT,
            {
                **state.context(),
                "remaining_budget": runtime.remaining(),
                "outline": state.plan.model_dump() if state.plan else None,
                "completed_sections": list(state.sections),
                "failed_sections": state.failures,
            },
            tools=runtime.toolset.schemas(data["web_enabled"]),
            conversation=state.observations(),
            count_native_tools=False,
            output_limit=6000,
        )
        calls = message.get("tool_calls") or []
        if not calls:
            if not state.sections:
                state.direct_body = markdown(message, document=True)
            runtime.complete()
            return {"pending": []}
        # The graph checkpoints each tool observation, including a batch selected in one decision.
        clean_calls = []
        if len(calls) > 32:
            raise ProjectError("TOOL_BUDGET", "本轮操作过多，已保留完成内容", 503)
        for index, call in enumerate(calls):
            function = call.get("function") or {}
            clean_calls.append(
                {
                    "id": call.get("id") or f"draft-{len(state.messages)}-{index}",
                    "type": "function",
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    },
                }
            )
        state.messages.append({"role": "assistant", "content": "", "tool_calls": clean_calls})
        return {"pending": clean_calls}

    async def tools(self, data):
        runtime, state = self, self.state
        call, *pending = data["pending"]
        finalization = (
            call["function"]["name"] == "finish_draft"
            and bool(state.sections)
            and not state.finalization_attempted
        )
        if state.status != "working":
            result = {"ok": False, "code": "DRAFT_ALREADY_FINISHED"}
        elif runtime.remaining()["tool_calls"] <= 0 and not finalization:
            state.status, state.stop_code = "partial", "TOOL_BUDGET"
            result = {"ok": False, "code": "TOOL_BUDGET"}
            await self.flow.event(
                "draft_agent", "操作次数已用完，保留已完成内容", ok=False, code="TOOL_BUDGET"
            )
        else:
            if finalization:
                state.finalization_attempted = True
            else:
                self.flow.calls += 1
            result = await runtime.toolset.call(call["function"], data["web_enabled"])
        state.messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": tool_json(result)}
        )
        return {"pending": pending}

    async def finalize(self, data):
        state = self.state
        if state.status == "working":
            self.complete()
        if state.status == "partial" and not state.stop_code:
            await self.flow.event(
                "draft_agent", "本次草案仍有章节待完善", ok=False, code="DRAFT_PARTIAL"
            )
        return {"body": self.assemble(), "partial": state.status != "completed"}

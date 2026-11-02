import asyncio
import hashlib
import json
import re
import time

from backend.agents.common import tool_json
from backend.agents.drafting.prompts import AGENT, CONTINUE, REVISION_AGENT, REVISION_VERIFY
from backend.agents.drafting.state import DraftState
from backend.agents.tools.drafting import RECOVERABLE, DraftTools, error_code, markdown, recoverable
from backend.models.documents import validate_revision_markdown
from backend.models.drafting import RevisionVerification
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
        if state.handoff:
            self.deadline = min(self.deadline, now + state.handoff.budget.seconds())

    def remaining(self):
        settings = self.flow.agent.settings
        tokens = max(0, settings.draft_token_budget - self.flow.tokens)
        models = max(0, settings.draft_max_rounds - self.flow.rounds)
        tools = max(0, settings.draft_max_tools - self.flow.calls)
        if self.state.handoff:
            base = self.state.handoff_counters
            budget = self.state.handoff.budget.remaining(
                models=max(0, self.flow.rounds - base["models"]),
                tools=max(0, self.flow.calls - base["tools"]),
                tokens=max(0, self.flow.tokens - base["tokens"]),
            )
            tokens, models, tools = (
                min(tokens, budget.tokens),
                min(models, budget.model_calls),
                min(tools, budget.tool_calls),
            )
        return {
            "model_calls": models,
            "tool_calls": tools,
            "tokens": tokens,
            "missing_sections": [s.model_dump() for s in self.missing()],
            "estimated_writer_capacity": min(
                models,
                tokens // 12000,
            ),
            "finalization_available": not self.state.finalization_attempted,
            "seconds": max(0, round(self.deadline - asyncio.get_running_loop().time(), 1)),
        }

    async def model(self, system, data, *, purpose="draft_planner", **kwargs):
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining < 1:
            raise ProjectError("DRAFT_TIME_BUDGET", "已保留完成内容，其余章节待完善", 503)
        if self.remaining()["model_calls"] < 1 or self.remaining()["tokens"] < 256:
            raise ProjectError("DRAFT_BUDGET", "本轮剩余预算不足，原有资料保留", 503)
        async with asyncio.timeout(
            min(remaining, self.flow.agent.settings.draft_model_timeout_seconds)
        ):
            return await self.flow.model(
                system, data, purpose=purpose, inherited_budget=self.remaining(), **kwargs
            )

    def complete(self, questions=()):
        self.state.questions = list(dict.fromkeys([*self.state.questions, *questions]))[:70]
        missing = self.missing()
        self.state.status = "partial" if missing or self.state.failures else "completed"

    def missing(self):
        if not self.state.plan:
            return []
        return [s for s in self.state.plan.sections if s.id not in self.state.sections]

    def assemble(self):
        if self.state.original_blocks:
            parts = []
            for block in self.state.original_blocks:
                body = self.state.sections.get(block["id"])
                if body is None:
                    parts.append(block["content"])
                else:
                    if self.state.incremental:
                        parts.append(body)
                        continue
                    heading = block["content"].splitlines()[0]
                    parts.append(heading + "\n\n" + body.strip() + "\n\n")
            return "".join(parts)
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

    async def prepare_document(self, data):
        state = self.state
        if state.incremental:
            original = "".join(b["content"] for b in state.original_blocks)
            if hashlib.sha256(original.encode()).hexdigest() != state.replacement["hash"]:
                raise ProjectError("VERSION_CONFLICT", "原稿与任务快照不一致", 409)
            protected = set(state.fixed_protected)
            for block in state.original_blocks:
                if re.search(r"客户原话|用户原始要求|原始需求原文", block["title"]):
                    protected.add(block["id"])
                for clause in re.split(r"[，,。；;\n]", state.user_request):
                    locked = re.search(r"(?:不要改|别改|不能改|不改|不动)(.+)", clause)
                    if locked and any(
                        len(term.strip()) >= 2 and term.strip() in block["title"]
                        for term in re.split(r"和|、|以及|及|与", locked[1])
                    ):
                        protected.add(block["id"])
            state.fixed_protected = sorted(protected)
            await self.flow.event("prepare_document", "正在定位本次调整涉及的内容")
        return {}

    async def verify(self, data):
        state = self.state
        if not state.incremental:
            return {}
        plan = state.revision_plan
        if (
            state.status == "partial"
            or not plan
            or state.failures
            or set(plan.targets) != set(state.sections)
            or not set([*plan.targets, *plan.related]) <= set(state.read_ids)
            or not state.read_ids
            or set(plan.targets) & set([*plan.protected, *state.fixed_protected])
        ):
            state.status, state.stop_code = "partial", "REVISION_INCOMPLETE"
            return {}
        if state.verification_attempted:
            return {}
        for block in state.original_blocks:
            if block["id"] in state.sections and state.sections[block["id"]] != block["content"]:
                validate_revision_markdown(state.sections[block["id"]])
        state.verification_attempted = True
        originals = [b for b in state.original_blocks if b["id"] in state.read_ids]
        if sum(len(b["content"]) for b in originals) > 24000:
            raise ProjectError("REVISION_SCOPE_LIMIT", "关联资料超过本轮核对容量，原稿保留", 503)
        await self.flow.event("verify", "正在核对关联规则与未改内容")
        response = await self.model(
            REVISION_VERIFY,
            {
                "request": state.user_request,
                "facts": state.business_notes,
                "plan": plan.model_dump(),
                "originals": [{"id": b["id"], "content": b["content"]} for b in originals],
                "replacements": state.sections,
                "protected_ids": state.fixed_protected,
            },
            purpose="draft_reviewer",
            schema=RevisionVerification,
            output_limit=1200,
        )
        result = RevisionVerification.model_validate(json.loads(response.get("content") or ""))
        state.verified = result.passed
        state.no_change = self.assemble() == "".join(b["content"] for b in state.original_blocks)
        state.status = "completed" if result.passed else "partial"
        if not result.passed:
            state.stop_code = "REVISION_VERIFY_FAILED"
            await self.flow.event("verify", "关联核对尚未通过，原稿保持不变", ok=False)
        return {}

    def restore(self, data):
        restored = DraftState.model_validate(data["draft"])
        if self.state.handoff:
            if restored.handoff and restored.handoff.fingerprint != self.state.handoff.fingerprint:
                raise ProjectError("HANDOFF_CONFLICT", "需求生成任务资料已变化，原稿保留", 409)
            if not restored.handoff:
                restored.handoff = self.state.handoff
                restored.handoff_counters = self.state.handoff_counters
        for name in DraftState.model_fields:
            setattr(self.state, name, getattr(restored, name))
        if self.state.handoff:
            self.flow.document_task = self.state.handoff
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
        if state.status != "working":
            return {"pending": []}
        if runtime.remaining()["model_calls"] <= 0:
            raise ProjectError("MODEL_BUDGET", "本轮次数已用完", 503)
        await self.flow.event("draft_decide", "起草助手判断下一步需要处理的内容")
        message = await runtime.model(
            REVISION_AGENT if state.incremental else CONTINUE if state.plan else AGENT,
            {
                **state.context(),
                "remaining_budget": runtime.remaining(),
                "outline": {
                    "sections": [section.model_dump() for section in state.plan.sections],
                    "pending_questions": state.plan.pending_questions,
                }
                if state.plan
                else None,
                "completed_sections": list(state.sections),
                "failed_sections": state.failures,
            },
            tools=runtime.toolset.schemas(data["web_enabled"]),
            conversation=state.observations(),
            count_native_tools=False,
            output_limit=2500,
        )
        calls = message.get("tool_calls") or []
        if not calls:
            if not state.sections and not state.original_blocks:
                state.direct_body = markdown(message, document=True)
            if state.original_blocks and not state.sections and not state.revision_plan:
                raise ProjectError("DRAFT_REVISION_EMPTY", "没有完成章节补写，原稿保持不变", 503)
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

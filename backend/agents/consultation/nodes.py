import json
import logging
import re
from time import monotonic
from uuid import UUID

import httpx
from langgraph.types import interrupt
from pydantic import ValidationError

from backend.agents.common import (
    measured_tokens,
    request_budget,
    tool_json,
)
from backend.agents.consultation.prompts import ADVISE, REPLY
from backend.agents.consultation.state import (
    ConsultationPayload,
    document_action,
    limit_questions,
    source_catalog,
    valid_draft_request,
    verified_patch,
)
from backend.agents.tools.drafting import generate_prd
from backend.agents.tools.web_search import PUBLIC_TOPIC_TOOL, PUBLIC_TOPICS, search_public
from backend.db.tool_store import ToolStore
from backend.models.consultation import (
    ConsultationRecord,
    ConsultationState,
    ConsultationTurn,
    Extraction,
    Reference,
)
from backend.models.discovery import merge_discovery
from backend.models.drafting import DraftDecision
from backend.models.handoff import AgentResult
from backend.models.projects import ProjectError

logger = logging.getLogger("supportflow.consultation")

DRAFT_COMMAND = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我)?(?:现在|直接|先|重新)?(?:生成|整理|起草|出)"
    r"(?:一下|一份|份|一个|个)?(?:需求)?(?:草案|方案|需求书)(?:吧|一下|。|！|!|谢谢)*$"
)


def is_draft_command(text):
    return bool(DRAFT_COMMAND.fullmatch(text.strip()))


class ConsultationNodes:
    def __init__(self, agent, store, principal, project_id, run_id, snapshot):
        self.agent, self.store, self.principal = agent, store, principal
        self.project_id, self.run_id, self.snapshot = project_id, run_id, snapshot
        self.state = ConsultationState.model_validate(snapshot["project"].get("consultation") or {})
        self.notes = snapshot["project"]["discovery"]
        self.patches, self.drafts = [], []
        self.rounds, self.tokens, self.calls = 0, 0, 0
        self.extraction = None
        self.record_retried = False
        self.decision = DraftDecision()
        self.answer = ""
        self.discussion_saved = False
        self.expected_version = snapshot["project"]["version"]
        self.replacement = None
        self.document_action = None
        self.task = None
        self.handoff_outcome = "completed"
        self.saved_version = None

    async def event(self, node, summary, **extra):
        await self.store.event(
            self.run_id, {"type": "node", "node": node, "summary": summary, **extra}
        )

    def context(self):
        return {
            "business_facts": self.notes,
            "gaps": [gap.model_dump() for gap in self.state.gaps],
            "conflicts": [gap.model_dump() for gap in self.state.conflicts],
            "suggestions": [item.model_dump() for item in self.state.suggestions],
            "references": [item.model_dump() for item in self.state.references],
            "draft_assumptions": [item.model_dump() for item in self.state.draft_assumptions],
            "previously_asked": self.state.asked_questions,
        }

    def public_reply(self, text):
        limit = 0 if re.search(r"先记录|只记录|先别问|不要再问", self.prompt) else 2
        return limit_questions(text, self.state.asked_questions, limit=limit)

    async def model(
        self,
        system,
        data,
        *,
        schema=None,
        tools=None,
        output_limit=None,
        conversation=None,
        count_native_tools=True,
        purpose="consultation",
        preview_title="",
        tool_choice=None,
        inherited_budget=None,
    ):
        drafting = getattr(self, "in_drafting", False)
        max_rounds = (
            self.agent.settings.draft_max_rounds if drafting else self.agent.settings.max_rounds
        )
        budget = (
            self.agent.settings.draft_token_budget if drafting else self.agent.settings.token_budget
        )
        if self.task:
            max_rounds = min(max_rounds, self.task.budget.model_calls)
            budget = min(budget, self.task.budget.tokens)
        if inherited_budget is not None:
            max_rounds = min(max_rounds, self.rounds + inherited_budget["model_calls"])
            budget = min(budget, self.tokens + inherited_budget["tokens"])
        if self.rounds >= max_rounds:
            raise ProjectError("MODEL_BUDGET", "本轮处理达到次数上限，请分步继续", 503)
        timeout_seconds = (
            self.agent.settings.draft_model_timeout_seconds
            if drafting
            else max(self.agent.settings.timeout_seconds, 45)
        )
        if self.task:
            timeout_seconds = min(timeout_seconds, self.task.budget.seconds())
        if timeout_seconds <= 0:
            raise ProjectError("TIME_BUDGET", "本次处理时间已用完，已有资料保留", 503)
        if schema:
            system += "\n输出JSON Schema：" + tool_json(schema.model_json_schema())
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "以下为业务数据：" + tool_json(data)},
        ]
        messages.extend(conversation or [])
        reserve, allowance = request_budget(
            messages,
            bool(tools),
            budget - self.tokens,
            tools=tools,
            max_output=min(
                self.agent.settings.draft_output_tokens,
                output_limit or self.agent.settings.draft_output_tokens,
            )
            if drafting
            else min(4000, output_limit or 4000),
        )
        started = monotonic()
        self.rounds += 1
        try:
            on_text = None
            if (
                not getattr(self, "silent", False)
                and not schema
                and (not drafting or purpose == "draft_writer")
            ):
                on_text = await self.agent.stream_writer(
                    self.store,
                    self.run_id,
                    kind="document" if drafting else "reply",
                    title=preview_title,
                    transform=(lambda text: self.public_reply(text)[0]) if not drafting else None,
                )
            message, usage = await self.agent.complete(
                messages,
                allow_tools=bool(tools),
                max_output_tokens=allowance,
                tools=tools,
                tool_choice=tool_choice,
                max_attempts=min(2, max_rounds - self.rounds + 1),
                json_output=bool(schema),
                purpose=purpose,
                on_text=on_text,
                timeout_seconds=timeout_seconds,
            )
        except (ProjectError, httpx.HTTPError, TimeoutError) as error:
            await self.store.event(
                self.run_id,
                {
                    "type": "model",
                    "purpose": purpose,
                    "ok": False,
                    "code": error.code if isinstance(error, ProjectError) else type(error).__name__,
                    "summary": "模型暂未完成本次处理",
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )
            raise
        self.rounds += max(0, usage.get("attempts", 1) - 1)
        if count_native_tools:
            self.calls += len(message.get("tool_calls") or [])
        max_tools = (
            self.agent.settings.draft_max_tools if drafting else self.agent.settings.max_tools
        )
        if self.task:
            max_tools = min(max_tools, self.task.budget.tool_calls)
        if self.calls > max_tools:
            raise ProjectError("TOOL_BUDGET", "本轮工具调用达到上限，请分步处理", 503)
        used = measured_tokens(usage, reserve + allowance)
        self.tokens += used
        if self.tokens > budget:
            raise ProjectError("TOKEN_BUDGET", "本轮资料超过用量预算，请分步处理", 503)
        await self.store.event(
            self.run_id,
            {
                "type": "model",
                "model": usage.get("model"),
                "purpose": usage.get("purpose", purpose),
                "attempts": usage.get("attempts", 1),
                "fallback_from": usage.get("fallback_from"),
                "summary": "调用模型整理需求",
                "duration_ms": round((monotonic() - started) * 1000),
                "tokens": {"total_tokens": used},
            },
        )
        return message

    async def extract(self, flow):
        self.decision = DraftDecision()
        if self.options.get("intent") == "draft" or is_draft_command(self.prompt):
            if str(self.principal.account.id) != self.snapshot["project"]["owner_id"]:
                raise ProjectError("FORBIDDEN", "请由客户决定是否整理需求草案", 403)
            self.extraction = Extraction(
                reply="我先根据已有沟通整理一份方案，未确定的地方会单独列出。",
                gaps=self.state.gaps,
                conflicts=self.state.conflicts,
            )
            self.decision = DraftDecision(request_quote=self.prompt)
            self.state.draft_consent, self.state.draft_paused = True, False
        else:
            await self.event("extract", "结合已有沟通回应您的问题")
            tool = {
                "type": "function",
                "function": {
                    "name": "record_consultation",
                    "description": "整理增量业务信息和本次沟通建议，不发布或立项。",
                    "parameters": ConsultationRecord.model_json_schema(),
                },
            }
            data = {
                "previous": self.context(),
                "recent_messages": self.snapshot["messages"][-12:],
                "input": self.prompt,
                "source_catalog": source_catalog(
                    self.prompt, self.run_id, self.snapshot["messages"][-12:]
                ),
                "has_draft": any(row["kind"] == "specification" for row in self.snapshot["items"]),
                "draft_paused": self.state.draft_paused,
                "memory_pending": self.state.memory_pending or not bool(self.notes),
                "record_retry": getattr(self, "record_retried", False),
            }
            web_enabled = (
                self.options.get("web_search")
                and (await ToolStore(self.store.database).get(self.principal))["enabled"]
            )
            record_required = self.state.memory_pending or bool(
                re.search(r"纠正|更正|改用|首期|我们|公司|员工|我负责|希望|不能|必须", self.prompt)
            )
            choice = (
                {"type": "function", "function": {"name": "record_consultation"}}
                if record_required
                else None
            )
            message = await self.model(
                ADVISE,
                data,
                tools=[tool, PUBLIC_TOPIC_TOOL] if web_enabled else [tool],
                tool_choice="required" if web_enabled and record_required else choice,
            )
            web_calls = [
                call
                for call in message.get("tool_calls") or []
                if call.get("function", {}).get("name") == "web_search"
            ]
            if web_calls:
                references, ok = [], False
                try:
                    args = json.loads(web_calls[0]["function"]["arguments"])
                    if (
                        not web_enabled
                        or set(args) != {"topic"}
                        or args["topic"] not in PUBLIC_TOPICS
                    ):
                        raise ValueError("Invalid public topic")
                    if (await ToolStore(self.store.database).get(self.principal))["enabled"]:
                        references = await search_public(
                            self.agent.client, PUBLIC_TOPICS[args["topic"]]
                        )
                        ok = bool(references)
                except (ValueError, KeyError, TypeError, TimeoutError, httpx.HTTPError):
                    pass
                await self.store.event(
                    self.run_id,
                    {
                        "type": "tool",
                        "tool": "web_search",
                        "ok": ok,
                        "summary": "已查询公开参考" if ok else "公开资料暂不可用，继续讨论已有信息",
                    },
                )
                known = {row.id: row for row in self.state.references}
                for reference in references:
                    row = Reference.model_validate(reference)
                    known[row.id] = row
                self.state.references = list(known.values())[-10:]
                message = await self.model(
                    ADVISE,
                    {**data, "public_references": references, "research_done": True},
                    tools=[tool],
                    tool_choice=choice,
                )
            reply = message.get("content") or ""
            if not isinstance(reply, str):
                reply = ""
            self.extraction = Extraction(
                reply="已收到您的说明。", gaps=self.state.gaps, conflicts=self.state.conflicts
            )
            for call in message.get("tool_calls") or []:
                ok = False
                if call.get("function", {}).get("name") == "record_consultation":
                    try:
                        raw = json.loads(call["function"]["arguments"])
                        turn = (
                            ConsultationRecord.model_validate(raw).turn()
                            if "notes" not in raw
                            else ConsultationTurn.model_validate(raw)
                        )
                        self.decision = turn.decision
                        self.extraction = Extraction.model_validate(
                            {
                                **turn.model_dump(
                                    exclude={"decision", "reply"}, exclude_unset=True
                                ),
                                "reply": turn.reply or reply or "已收到您的说明。",
                            }
                        )
                        if not reply.strip():
                            reply = turn.reply
                        # An invalid control quote is ignored without discarding conversation.
                        for key, target in (
                            ("draft_request_quote", "request_quote"),
                            ("cancel_draft_quote", "pause_quote"),
                        ):
                            invalid_quote = False
                            quote = getattr(self.extraction, key)
                            if quote and (
                                quote not in self.prompt
                                or key == "draft_request_quote"
                                and not valid_draft_request(quote, self.prompt)
                            ):
                                setattr(self.extraction, key, "")
                                invalid_quote = True
                            candidate = getattr(self.decision, target)
                            if candidate and (
                                candidate not in self.prompt
                                or target == "request_quote"
                                and not valid_draft_request(candidate, self.prompt)
                            ):
                                setattr(self.decision, target, "")
                                invalid_quote = True
                            if invalid_quote:
                                await self.event(
                                    "assess",
                                    "未采纳未明确要求生成需求书的操作意愿",
                                    ok=False,
                                    code="CONTROL_QUOTE_INVALID",
                                )
                        try:
                            patch, sources = verified_patch(
                                self.extraction,
                                self.prompt,
                                self.run_id,
                                self.notes,
                                self.snapshot["messages"][-12:],
                            )
                        except ValueError:
                            await self.event(
                                "extract",
                                "本次说明保留在对话中，纪要待核对",
                                ok=False,
                                code="FACT_SOURCE_INVALID",
                            )
                            self.state.memory_pending = True
                        else:
                            self.notes = merge_discovery(self.notes, patch)
                            self.patches.append(patch)
                            removed = patch.get("remove_facts", {})
                            retained = [
                                s
                                for s in self.state.sources
                                if s.value not in removed.get(s.field, [])
                            ]
                            active = [s for s in sources if s.value not in removed.get(s.field, [])]
                            self.state.sources = [*retained, *active][-100:]
                            if any(
                                key != "open_questions" and value for key, value in patch.items()
                            ):
                                self.state.memory_pending = False
                            ok = True
                    except (ValueError, KeyError, TypeError) as error:
                        self.state.memory_pending = True
                        fields = (
                            ",".join(
                                str(item["loc"][0])
                                if item["loc"] and item["loc"][0] in ConsultationTurn.model_fields
                                else "unknown"
                                for item in error.errors(include_input=False)
                            )
                            if isinstance(error, ValidationError)
                            else "arguments"
                        )
                        logger.warning(
                            "consultation_tool_invalid run_id=%s code=RECORD_FORMAT fields=%s",
                            self.run_id,
                            fields[:200],
                        )
                await self.store.event(
                    self.run_id,
                    {
                        "type": "tool",
                        "tool": "record_consultation",
                        "ok": ok,
                        "code": "RECORDED" if ok else "RECORD_NOT_APPLIED",
                        "summary": "已整理本次业务信息" if ok else "业务纪要暂未更新，保留原有信息",
                    },
                )
                if not ok and not getattr(self, "record_retried", False):
                    self.record_retried = True
                    await self.event("extract", "正在核对本次业务记录")
                    return await self.extract(flow)
                # Only one record update is accepted per response.
                break
            if not reply.strip() or len(reply) > 900 or reply.lstrip().startswith(("{", "[")):
                previous_reply = reply
                try:
                    response = await self.model(
                        REPLY,
                        {
                            **data,
                            "verified_facts": self.notes,
                            "record_pending": self.state.memory_pending,
                        },
                        output_limit=900,
                    )
                    reply = response.get("content") or ""
                    if (
                        not isinstance(reply, str)
                        or not reply.strip()
                        or len(reply) > 1800
                        or reply.lstrip().startswith(("{", "["))
                    ):
                        raise ProjectError("REPLY_INVALID", "顾问暂未能完成回复", 503)
                except (ProjectError, httpx.HTTPError, TimeoutError):
                    if (
                        isinstance(previous_reply, str)
                        and previous_reply.strip()
                        and not previous_reply.lstrip().startswith(("{", "["))
                    ):
                        reply = previous_reply
                    else:
                        reply = (
                            "本次沟通已保留，顾问回复暂未完成。"
                            "您可以继续补充，无需重复提供这些信息。"
                        )
            reply, questions = self.public_reply(reply)
            if not reply:
                reply = "您的说明已保留，我们可以接着讨论您关心的内容。"
            self.state.asked_questions = [*self.state.asked_questions, *questions][-30:]
            if self.state.memory_pending:
                reply += "\n\n本次原话已保留，业务纪要还有内容未能更新，后续沟通时会继续核对。"
            self.extraction.reply = reply
        owner = str(self.principal.account.id) == self.snapshot["project"]["owner_id"]
        if owner:
            if self.extraction.draft_request_quote or self.decision.request_quote:
                self.state.draft_consent, self.state.draft_paused = True, False
            if self.extraction.cancel_draft_quote or self.decision.pause_quote:
                self.state.draft_consent, self.state.draft_paused = False, True
        accepted, rejected = (
            self.options.get("accepted_suggestions", []),
            self.options.get("rejected_suggestions", []),
        )
        if accepted or rejected:
            known = {item.id for item in self.state.suggestions}
            if not owner or set(accepted) & set(rejected) or not set(accepted + rejected) <= known:
                raise ProjectError("SUGGESTION_INVALID", "请由项目客户确认当前记录中的建议", 422)
            for item in self.state.suggestions:
                if item.id in accepted:
                    item.status = "accepted"
                elif item.id in rejected:
                    item.status = "rejected"
        if self.notes != self.snapshot["project"]["discovery"] and any(
            row["kind"] == "specification" for row in self.snapshot["items"]
        ):
            self.state.document_pending = True
        return {}

    async def decide_draft(self, flow):
        self.state.pending_search_query = ""
        self.state.gaps = self.extraction.gaps
        self.state.conflicts = self.extraction.conflicts
        requested = bool(
            self.options.get("intent") == "draft"
            or is_draft_command(self.prompt)
            or valid_draft_request(self.prompt, self.prompt)
            or valid_draft_request(self.extraction.draft_request_quote, self.prompt)
            or valid_draft_request(self.decision.request_quote, self.prompt)
        )
        if requested and str(self.principal.account.id) != self.snapshot["project"]["owner_id"]:
            raise ProjectError("FORBIDDEN", "请由客户决定是否整理需求草案", 403)
        generate = requested and not self.state.draft_paused
        if (
            requested
            and valid_draft_request(self.prompt, self.prompt)
            and not (self.extraction.cancel_draft_quote or self.decision.pause_quote)
        ):
            self.state.draft_paused = False
            generate = True
        self.document_action = (
            document_action(
                self.prompt,
                has_draft=any(row["kind"] == "specification" for row in self.snapshot["items"]),
            )
            if generate
            else None
        )
        if not generate and self.state.status != "draft_ready":
            self.state.status = "waiting_user"
        self.state.draft_consent = False
        await self.event("decide_draft", "按您的要求整理草案" if generate else "保存本次沟通信息")
        return {"route": "save_discussion" if generate else "wait_user"}

    async def save_discussion(self, flow):
        if any(row["kind"] == "specification" for row in self.snapshot["items"]):
            self.state.document_pending = True
        self.state.discussion_run_id = str(self.run_id)
        self.state.checkpoint_thread = self.thread
        self.answer = self.extraction.reply + "\n\n我会把这些信息整理成草案，供您继续核对。"
        self.expected_version = await self.store.finish(
            self.principal,
            self.project_id,
            self.run_id,
            self.snapshot["project"]["revision"],
            self.answer,
            [],
            self.patches,
            consultation=self.state.model_dump(mode="json"),
            expected_version=self.expected_version,
            defer_completion=True,
            discussion_only=True,
        )
        self.patches = []
        self.discussion_saved = True
        self.snapshot["project"]["version"] = self.expected_version
        return {}

    async def draft_agent(self, flow):
        try:
            result = await generate_prd(self)
            AgentResult.model_validate(result["handoff"]).validate_for(
                self.document_task, content=result["document"]["content"]
            )
            self.handoff_outcome = "partial" if result["partial"] else "completed"
        except (ProjectError, httpx.HTTPError, TimeoutError, ValueError) as error:
            self.handoff_outcome = "partial"
            if isinstance(error, ProjectError) and error.code == "BUSINESS_CONTEXT_REQUIRED":
                self.state.status = "waiting_user"
                self.answer = error.message
                return {}
            if isinstance(error, ProjectError) and error.status < 500:
                raise
            code = error.code if isinstance(error, ProjectError) else type(error).__name__
            logger.warning("draft_deferred run_id=%s code=%s", self.run_id, code)
            await self.event("draft_agent", "本次沟通已保存，草案暂未完成", ok=False, code=code)
            self.state.status, self.state.draft_consent = "ready", False
            self.answer = self.extraction.reply + (
                "\n\n这次沟通已保存，草案暂时没有整理完成，已有草案保留不变。"
                "您可以继续讨论，或选择重新整理，无需重复提供这些信息。"
            )
            return {}
        self.drafts = [result["document"]]
        self.state.document_pending = False
        if result.get("no_change"):
            self.drafts, self.replacement = [], None
            self.handoff_outcome = "no_change"
            self.state.status = "draft_ready"
            self.answer = "当前需求已经包含这次要求，无需修改；原文和确认状态保持不变。"
            return {}
        self.replacement = result.get("replacement")
        self.state.status, self.state.draft_consent = "draft_ready", False
        self.answer = (
            "已根据保存的业务信息整理并保存一份初稿，详细说明暂未全部生成，待完善处已注明。"
            if result["partial"]
            else (
                "已根据保存的业务信息生成并保存需求草案。"
                "未确定的细节放在待确认事项中，不影响先查看方案。"
            )
        ) + "您可以在“需求草案”中查看、修改；目前尚未立项或发布。"
        if self.replacement:
            self.answer = (
                "已更新需求书中的相关内容，其他正文保留；可查看修改差异，或撤销本次修改。"
                if self.replacement.get("incremental")
                else "已在当前草案中补写指定章节，其他章节保留；请查看最新草案。"
            )
        return {}

    async def wait_user(self, flow):
        await self.event("wait_user", "保存本次沟通，等待您继续")
        self.answer = self.extraction.reply
        return {}

    async def persist(self, flow):
        await self.event("hitl", "沟通已保存，等待您补充或查看草案")
        self.state.last_run_id = str(self.run_id)
        self.state.checkpoint_thread = self.thread
        self.saved_version = await self.store.finish(
            self.principal,
            self.project_id,
            self.run_id,
            self.snapshot["project"]["revision"],
            self.answer,
            self.drafts,
            self.patches,
            consultation=self.state.model_dump(mode="json"),
            expected_version=self.expected_version,
            defer_completion=True,
            continue_discussion=self.discussion_saved,
            replacement=self.replacement,
        )
        return {}

    async def human_input(self, flow):
        resumed = interrupt(
            {
                "kind": "draft_review" if self.state.status == "draft_ready" else "clarification",
                "project_id": str(self.project_id),
                "questions": [gap.question for gap in self.state.gaps[:2]],
                "message": "等待客户补充或人工审阅，本次模型执行已结束",
            }
        )
        self.run_id, self.prompt, self.options = (
            UUID(resumed["run_id"]),
            resumed["prompt"],
            resumed["options"],
        )
        self.rounds, self.tokens, self.calls = 0, 0, 0
        self.patches, self.drafts = [], []
        self.extraction = None
        self.record_retried = False
        self.answer = ""
        self.discussion_saved = False
        self.replacement = None
        self.document_action = None
        self.notes = self.snapshot["project"]["discovery"]
        self.handoff_outcome = "completed"
        self.state = ConsultationState.model_validate(
            self.snapshot["project"].get("consultation") or {}
        )
        self.expected_version = self.snapshot["project"]["version"]
        return {"route": "extract"}

    def checkpoint_payload(self):
        return ConsultationPayload.model_validate(
            {
                "run_id": str(self.run_id),
                "prompt": self.prompt,
                "options": self.options,
                "notes": self.notes,
                "state": self.state.model_dump(mode="json"),
                "patches": self.patches,
                "drafts": self.drafts,
                "replacement": self.replacement,
                "document_action": self.document_action,
                "handoff_outcome": self.handoff_outcome,
                "rounds": self.rounds,
                "tokens": self.tokens,
                "calls": self.calls,
                "record_retried": self.record_retried,
                "extraction": self.extraction.model_dump(exclude_unset=True)
                if self.extraction
                else None,
                "answer": self.answer,
                "decision": self.decision.model_dump(),
                "discussion_saved": self.discussion_saved,
                "expected_version": self.expected_version,
            }
        ).model_dump(mode="json", exclude_unset=True)

    def restore(self, payload):
        if not payload:
            return
        payload = ConsultationPayload.model_validate(payload).model_dump(
            mode="json", exclude_unset=True
        )
        for field in (
            "prompt",
            "options",
            "notes",
            "patches",
            "drafts",
            "rounds",
            "tokens",
            "calls",
            "answer",
        ):
            setattr(self, field, payload[field])
        self.decision = DraftDecision.model_validate(payload.get("decision") or {})
        self.record_retried = payload.get("record_retried", False)
        self.replacement = payload.get("replacement")
        self.document_action = payload.get("document_action")
        self.handoff_outcome = payload.get("handoff_outcome", "completed")
        self.discussion_saved = payload.get("discussion_saved", False)
        self.expected_version = payload.get("expected_version", self.snapshot["project"]["version"])
        self.run_id = UUID(payload["run_id"])
        self.state = ConsultationState.model_validate(payload["state"])
        self.extraction = (
            Extraction.model_validate(payload["extraction"]) if payload["extraction"] else None
        )

    def node(self, name):
        async def invoke(flow):
            self.restore(flow.get("payload"))
            result = await getattr(self, name)(flow)
            if name == "persist":
                # The committed document lives in PG; waiting does not need another full copy.
                self.patches, self.drafts = [], []
                self.extraction, self.replacement = None, None
                self.answer = ""
                self.decision = DraftDecision()
                self.document_action = None
            return {**result, "payload": self.checkpoint_payload()}

        return invoke


class FactNodes(ConsultationNodes):
    """Silent customer-fact branch; no reply, draft, search or approval tool is exposed."""

    def __init__(self, agent, store, principal, project_id, job):
        snapshot = {
            "project": job["project"],
            "messages": job["messages"],
            "runs": [],
            "items": [],
            "baseline": None,
        }
        super().__init__(agent, store, principal, project_id, UUID(job["run_id"]), snapshot)
        self.job, self.silent, self.prompt, self.options = job, True, "", {}

    async def extract_facts(self, state):
        from backend.agents.consultation.prompts import EXTRACT_FACTS

        tool = {
            "type": "function",
            "function": {
                "name": "record_consultation",
                "description": "静默整理客户新说明，不回复、不生成文档。",
                "parameters": ConsultationRecord.model_json_schema(),
            },
        }
        data = {
            "previous": self.notes,
            "customer_messages": [
                {"id": row["id"], "content": row["content"]} for row in self.job["messages"]
            ],
            "source_catalog": source_catalog("", self.run_id, self.job["messages"]),
        }
        for attempt in range(2):
            message = await self.model(
                EXTRACT_FACTS,
                {**data, "record_retry": bool(attempt)},
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": "record_consultation"}},
            )
            try:
                call = next(
                    c
                    for c in message.get("tool_calls", [])
                    if c.get("function", {}).get("name") == "record_consultation"
                )
                record = ConsultationRecord.model_validate_json(call["function"]["arguments"])
                repeated = [
                    fact
                    for fact in record.facts
                    if self.notes.get(fact.field) == fact.value
                    or isinstance(self.notes.get(fact.field), list)
                    and fact.value in self.notes[fact.field]
                ]
                if record.facts and len(repeated) == len(record.facts) and not record.remove_facts:
                    raise ValueError("EXISTING_FACTS_ONLY")
                record.facts = [fact for fact in record.facts if fact not in repeated]
                extraction = record.turn()
                extraction.draft_request_quote = extraction.cancel_draft_quote = ""
                patch, sources = verified_patch(
                    extraction,
                    "",
                    self.run_id,
                    self.notes,
                    self.job["messages"],
                    current_message_ids={row["id"] for row in self.job["messages"]},
                )
                return {"patch": patch, "sources": [s.model_dump(mode="json") for s in sources]}
            except (StopIteration, ValueError, KeyError, TypeError) as error:
                data["record_errors"] = (
                    [
                        {"field": list(item["loc"]), "type": item["type"]}
                        for item in error.errors(include_input=False, include_url=False)
                    ]
                    if isinstance(error, ValidationError)
                    else [
                        {
                            "type": type(error).__name__,
                            "reason": (
                                "只重复旧纪要：请重新阅读本批原话，提取尚未记录的新规则；"
                                "确实没有新事实才返回空facts，不要把旧事实错误挂到新消息。"
                                if str(error) == "EXISTING_FACTS_ONLY"
                                else "核对field、value与真实source_id"
                            ),
                        }
                    ]
                )
                logger.warning(
                    "silent_record_invalid run_id=%s attempt=%s error_type=%s",
                    self.run_id,
                    attempt + 1,
                    type(error).__name__,
                )
                if attempt:
                    raise ProjectError(
                        "FACT_EXTRACTION_INVALID", "客户信息待继续整理，聊天记录已保存", 503
                    ) from None

    async def persist_facts(self, state):
        from backend.models.consultation import FactSource

        saved_version = await self.store.finish_facts(
            self.principal,
            self.project_id,
            self.job,
            state["patch"],
            [FactSource.model_validate(s) for s in state["sources"]],
        )
        return {"patch": {}, "sources": [], "saved_version": saved_version}

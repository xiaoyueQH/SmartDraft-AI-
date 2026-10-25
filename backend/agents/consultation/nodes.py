import json
import logging
import re
from time import monotonic
from uuid import UUID

import httpx
from langgraph.types import interrupt
from pydantic import ValidationError

from backend.agents.consultation.prompts import ADVISE, REPLY
from backend.agents.consultation.validation import (
    source_catalog,
    verified_patch,
)
from backend.agents.limits import measured_tokens, request_budget
from backend.agents.tools.draft_agent import generate_prd
from backend.agents.tools.registry import tool_json
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
        self.decision = DraftDecision()
        self.answer = ""
        self.discussion_saved = False
        self.expected_version = snapshot["project"]["version"]

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
        }

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
    ):
        drafting = getattr(self, "in_drafting", False)
        max_rounds = (
            self.agent.settings.draft_max_rounds if drafting else self.agent.settings.max_rounds
        )
        budget = (
            self.agent.settings.draft_token_budget if drafting else self.agent.settings.token_budget
        )
        if self.rounds >= max_rounds:
            raise ProjectError("MODEL_BUDGET", "本轮处理达到次数上限，请分步继续", 503)
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
            message, usage = await self.agent.complete(
                messages,
                allow_tools=bool(tools),
                max_output_tokens=allowance,
                tools=tools,
                json_output=bool(schema),
                timeout_seconds=self.agent.settings.draft_model_timeout_seconds
                if drafting
                else max(self.agent.settings.timeout_seconds, 45),
            )
        except (ProjectError, httpx.HTTPError, TimeoutError) as error:
            await self.store.event(
                self.run_id,
                {
                    "type": "model",
                    "ok": False,
                    "code": error.code if isinstance(error, ProjectError) else type(error).__name__,
                    "summary": "模型暂未完成本次处理",
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )
            raise
        if count_native_tools:
            self.calls += len(message.get("tool_calls") or [])
        if self.calls > (
            self.agent.settings.draft_max_tools if drafting else self.agent.settings.max_tools
        ):
            raise ProjectError("TOOL_BUDGET", "本轮工具调用达到上限，请分步处理", 503)
        used = measured_tokens(usage, reserve + allowance)
        self.tokens += used
        if self.tokens > budget:
            raise ProjectError("TOKEN_BUDGET", "本轮资料超过用量预算，请分步处理", 503)
        await self.store.event(
            self.run_id,
            {
                "type": "model",
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
            }
            web_enabled = (
                self.options.get("web_search")
                and (await ToolStore(self.store.database).get(self.principal))["enabled"]
            )
            message = await self.model(
                ADVISE, data, tools=[tool, PUBLIC_TOPIC_TOOL] if web_enabled else [tool]
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
                            quote = getattr(self.extraction, key)
                            if quote and quote not in self.prompt:
                                setattr(self.extraction, key, "")
                            candidate = getattr(self.decision, target)
                            if candidate and candidate not in self.prompt:
                                setattr(self.decision, target, "")
                                await self.event(
                                    "assess",
                                    "未采纳缺少原话依据的操作意愿",
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
                            self.state.sources = [*self.state.sources, *sources][-100:]
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
        return {}

    async def decide_draft(self, flow):
        self.state.pending_search_query = ""
        self.state.gaps = self.extraction.gaps
        self.state.conflicts = self.extraction.conflicts
        requested = bool(
            self.options.get("intent") == "draft"
            or is_draft_command(self.prompt)
            or self.extraction.draft_request_quote
            or self.decision.request_quote
        )
        if requested and str(self.principal.account.id) != self.snapshot["project"]["owner_id"]:
            raise ProjectError("FORBIDDEN", "请由客户决定是否整理需求草案", 403)
        generate = requested and not self.state.draft_paused
        if not generate and self.state.status != "draft_ready":
            self.state.status = "waiting_user"
        self.state.draft_consent = False
        await self.event("decide_draft", "按您的要求整理草案" if generate else "保存本次沟通信息")
        return {"route": "save_discussion" if generate else "wait_user"}

    async def save_discussion(self, flow):
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
        except (ProjectError, httpx.HTTPError, TimeoutError, ValueError) as error:
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
        self.state.status, self.state.draft_consent = "draft_ready", False
        self.answer = (
            "已根据保存的业务信息整理并保存一份初稿，详细说明暂未全部生成，待完善处已注明。"
            if result["partial"]
            else (
                "已根据保存的业务信息生成并保存需求草案。"
                "未确定的细节放在待确认事项中，不影响先查看方案。"
            )
        ) + "您可以在“需求草案”中查看、修改；目前尚未立项或发布。"
        return {}

    async def wait_user(self, flow):
        await self.event("wait_user", "保存本次沟通，等待您继续")
        self.answer = self.extraction.reply
        return {}

    async def persist(self, flow):
        await self.event("hitl", "沟通已保存，等待您补充或查看草案")
        self.state.last_run_id = str(self.run_id)
        self.state.checkpoint_thread = self.thread
        await self.store.finish(
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
        self.answer = ""
        self.discussion_saved = False
        self.notes = self.snapshot["project"]["discovery"]
        self.state = ConsultationState.model_validate(
            self.snapshot["project"].get("consultation") or {}
        )
        self.expected_version = self.snapshot["project"]["version"]
        return {"route": "extract"}

    def checkpoint_payload(self):
        return {
            "run_id": str(self.run_id),
            "prompt": self.prompt,
            "options": self.options,
            "notes": self.notes,
            "state": self.state.model_dump(mode="json"),
            "patches": self.patches,
            "drafts": self.drafts,
            "rounds": self.rounds,
            "tokens": self.tokens,
            "calls": self.calls,
            "extraction": self.extraction.model_dump(exclude_unset=True)
            if self.extraction
            else None,
            "answer": self.answer,
            "decision": self.decision.model_dump(),
            "discussion_saved": self.discussion_saved,
            "expected_version": self.expected_version,
        }

    def restore(self, payload):
        if not payload:
            return
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
            return {**result, "payload": self.checkpoint_payload()}

        return invoke

import json
import re

import httpx
from pydantic import ValidationError

from backend.agents.drafting.prompts import REVISION_SECTION, SECTION
from backend.agents.tools.web_search import PUBLIC_TOPIC_TOOL, PUBLIC_TOPICS, search_public
from backend.db.tool_store import ToolStore
from backend.models.consultation import Reference
from backend.models.drafting import (
    DraftFinish,
    DraftPlan,
    DraftSection,
    ReadRevisionBlocks,
    RevisionPlan,
    WriteSection,
)
from backend.models.projects import ProjectError

RECOVERABLE = (httpx.HTTPError, TimeoutError, ProjectError, ValueError)
EXHAUSTED = {"MODEL_BUDGET", "TOOL_BUDGET", "TOKEN_BUDGET", "DRAFT_TIME_BUDGET"}


def error_code(error):
    return error.code if isinstance(error, ProjectError) else type(error).__name__


def recoverable(error):
    return not isinstance(error, ProjectError) or error.status >= 500


def markdown(message, *, document=False):
    body = message.get("content")
    if (
        not isinstance(body, str)
        or len(body.strip()) < 100
        or body.lstrip().startswith(("{", "[", "```"))
        or (document and not body.lstrip().startswith("#"))
    ):
        raise ProjectError("DRAFT_TEXT_INVALID", "草案正文暂未生成", 503)
    return body.strip()


def tool(name, description, schema):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


class DraftTools:
    def __init__(self, runtime):
        self.runtime, self.state, self.flow = runtime, runtime.state, runtime.flow

    def schemas(self, web_enabled):
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        if self.state.incremental:
            choices = [
                tool(
                    "plan_revision",
                    "规划直接修改、关联核对和受保护的原块ID",
                    RevisionPlan.model_json_schema(),
                ),
                tool(
                    "read_revision_blocks",
                    "读取本次必需原文，每次最多4块，不截断",
                    ReadRevisionBlocks.model_json_schema(),
                ),
                tool(
                    "write_prd_section",
                    "只更新已规划且已读取的原块，返回完整块",
                    WriteSection.model_json_schema(),
                ),
                tool(
                    "finish_draft",
                    "结束本次局部更新，随后统一核对",
                    DraftFinish.model_json_schema(),
                ),
            ]
            return [*choices, PUBLIC_TOPIC_TOOL] if web_enabled else choices
        tools = [
            tool(
                "plan_draft",
                "组织大纲、统一角色和跨章节设计约定；不修改客户事实。",
                DraftPlan.model_json_schema(),
            ),
            tool(
                "write_prd_section",
                "按大纲返回的section_id撰写一章，章名和重点从共享状态读取，不必重新抄写。",
                WriteSection.model_json_schema(),
            ),
            tool("read_draft", "查看本次已写正文和待完善章节。", empty),
            tool(
                "finish_draft",
                "结束本次起草，汇总已写章节；未写完的章节保留待完善。",
                DraftFinish.model_json_schema(),
            ),
        ]
        return [*tools, PUBLIC_TOPIC_TOOL] if web_enabled else tools

    async def call(self, function, web_enabled):

        name, ok = function["name"], False
        try:
            args = json.loads(function["arguments"])
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            if self.state.incremental and name not in {
                "plan_revision",
                "read_revision_blocks",
                "write_prd_section",
                "finish_draft",
                "web_search",
            }:
                raise ValueError("局部更新不提供全文替代工具")
            if self.state.incremental and name == "plan_revision":
                plan = RevisionPlan.model_validate(args)
                known = {b["id"] for b in self.state.original_blocks}
                if (
                    self.state.section_specs
                    or not set([*plan.targets, *plan.related, *plan.protected]) <= known
                ):
                    raise ValueError("范围无效或写作已开始")
                if set(plan.targets) & set(self.state.fixed_protected):
                    raise ValueError("目标包含客户禁止改动或原话块")
                self.state.revision_plan = plan
                result = {"ok": True, "targets": plan.targets, "related": plan.related}
            elif self.state.incremental and name == "read_revision_blocks":
                request = ReadRevisionBlocks.model_validate(args)
                rows = {b["id"]: b for b in self.state.original_blocks}
                if (
                    not set(request.ids) <= set(rows)
                    or sum(len(rows[s]["content"]) for s in request.ids) > 24000
                ):
                    raise ValueError("读取范围无效或过大")
                self.state.read_ids = list(dict.fromkeys([*self.state.read_ids, *request.ids]))
                result = {
                    "ok": True,
                    "blocks": [{"id": sid, "content": rows[sid]["content"]} for sid in request.ids],
                }
            elif self.state.incremental and name == "plan_draft":
                raise ValueError("局部修订不能重规划整篇")
            elif name == "plan_draft":
                plan = DraftPlan.model_validate(args)
                if self.state.original_blocks:
                    originals = {b["id"]: b for b in self.state.original_blocks}
                    if any(s.id not in originals for s in plan.sections):
                        raise ValueError("Revision must use existing section IDs")
                    for section in plan.sections:
                        section.title = originals[section.id]["title"]
                # Already written/attempted chapters cannot silently disappear from the plan.
                known = {s.id: s for s in plan.sections}
                if any(known.get(sid) != spec for sid, spec in self.state.section_specs.items()):
                    raise ValueError("Cannot replace attempted sections")
                if self.state.section_specs:
                    previous_rules = self.state.plan.design_rules if self.state.plan else []
                    previous_roles = (
                        self.state.plan.role_terms if self.state.plan else []
                    ) or self.state.business_notes.get("users", [])
                    next_roles = plan.role_terms or self.state.business_notes.get("users", [])
                    if plan.design_rules != previous_rules or next_roles != previous_roles:
                        raise ValueError("Cannot change shared design after writing starts")
                self.state.plan = plan
                self.state.assumptions = plan.assumptions
                self.resolve(plan.resolved_questions)
                result = {"ok": True, "sections": [s.model_dump() for s in plan.sections]}
            elif name == "write_prd_section":
                result = await self.write(WriteSection.model_validate(args))
            elif name == "web_search":
                result = await self.search(args, web_enabled)
            elif name == "read_draft" and not args:
                result = {
                    "ok": True,
                    "body": self.runtime.assemble()[:12000],
                    "failed": self.state.failures,
                }
            elif name == "finish_draft":
                request = DraftFinish.model_validate(args)
                if not self.state.sections and not self.state.revision_plan:
                    raise ValueError("No completed section; write or return full Markdown")
                if request.assumptions:
                    self.state.assumptions = request.assumptions
                self.resolve(request.resolved_questions)
                self.runtime.complete(request.questions)
                result = {"ok": True, "partial": self.state.status == "partial"}
            else:
                raise ValueError("Tool unavailable")
            ok = result.get("ok", False)
        except ValidationError as error:
            known = (
                set(DraftPlan.model_fields)
                | set(WriteSection.model_fields)
                | set(DraftFinish.model_fields)
            )
            fields = list(
                dict.fromkeys(
                    str(item["loc"][0]) if item["loc"] and item["loc"][0] in known else "arguments"
                    for item in error.errors(include_input=False)
                )
            )[:5]
            result = {
                "ok": False,
                "code": "DRAFT_TOOL_ARGUMENTS",
                "fields": fields,
                "message": "请按Schema补齐字段。写章仅需section_id；大纲sections每项至少有title。",
            }
        except (ValueError, TypeError, KeyError):
            result = {
                "ok": False,
                "code": "DRAFT_TOOL_ARGUMENTS",
                "message": "工具参数无效，原资料未改变；可继续其他工作或结束。",
            }
        except (ProjectError, httpx.HTTPError, TimeoutError) as error:
            if not recoverable(error) or (
                isinstance(error, ProjectError) and error.code in EXHAUSTED
            ):
                raise
            result = {
                "ok": False,
                "code": error_code(error),
                "message": "本次工具未完成，已保留已有内容，可继续其他工作或结束。",
            }
        await self.flow.store.event(
            self.flow.run_id,
            {
                "type": "tool",
                "tool": name,
                "ok": ok,
                "code": result.get("code", "OK" if ok else "DRAFT_TOOL_FAILED"),
                "summary": "已处理本次起草操作" if ok else "本次操作未完成，原资料保留",
            },
        )
        return result

    def resolve(self, questions):
        known = self.state.business_notes.get("open_questions", [])
        self.state.resolved_questions = list(
            dict.fromkeys(
                [
                    *self.state.resolved_questions,
                    *(q for q in questions if q in known),
                ]
            )
        )

    async def write(self, request):
        if self.state.incremental:
            return await self.write_revision(request)

        spec = (
            next((s for s in self.state.plan.sections if s.id == request.section_id), None)
            if self.state.plan
            else None
        )
        if spec is None and self.state.plan:
            # Display titles/ordinals are aliases within this invocation, never external IDs.
            matches = [s for s in self.state.plan.sections if s.title == request.section_id]
            ordinal = re.fullmatch(r"(?:section[-_]?)?([1-6])", request.section_id, re.I)
            if len(matches) == 1:
                spec = matches[0]
            elif ordinal and int(ordinal[1]) <= len(self.state.plan.sections):
                spec = self.state.plan.sections[int(ordinal[1]) - 1]
        if spec is None and self.state.plan:
            return {
                "ok": False,
                "code": "SECTION_NOT_FOUND",
                "section_ids": [s.id for s in self.state.plan.sections],
                "message": "请使用大纲返回的section_id。",
            }
        if spec is None:
            original = next(
                (b for b in self.state.original_blocks if b["id"] == request.section_id), None
            )
            if original:
                spec = DraftSection(
                    id=original["id"],
                    title=original["title"],
                    focus=request.focus or self.state.user_request,
                )
            elif self.state.original_blocks:
                return {
                    "ok": False,
                    "code": "SECTION_NOT_FOUND",
                    "message": "补写请使用existing_sections中的原章节ID。",
                }
        if spec is None:
            if not request.title:
                raise ValueError("A standalone section needs a title")
            spec = DraftSection(
                id=request.section_id, title=request.title, focus=request.focus or request.title
            )
        if spec.id in self.state.section_specs:
            return {
                "ok": spec.id in self.state.sections,
                "code": "SECTION_ALREADY_ATTEMPTED",
                "message": "本章已处理，不再重复调用模型。",
            }
        if len(self.state.section_specs) >= 6:
            raise ValueError("Section capacity")
        if any(s.title == spec.title for s in self.state.section_specs.values()):
            raise ValueError("Duplicate section")
        self.state.section_specs[spec.id] = spec
        try:
            await self.flow.event("draft_section", "展开本章功能与验收方式")
            response = await self.runtime.model(
                SECTION,
                {
                    **self.state.context(include_written=True),
                    "current_section": spec.model_dump(),
                    "outline": [s.model_dump() for s in self.state.plan.sections]
                    if self.state.plan
                    else None,
                    "original_section": next(
                        (b["content"] for b in self.state.original_blocks if b["id"] == spec.id), ""
                    ),
                },
                output_limit=self.flow.agent.settings.draft_section_output_tokens,
                purpose="draft_writer",
                preview_title=spec.title,
            )
            body = markdown(response)
            body = re.sub(r"\A#{1,2}\s+" + re.escape(spec.title) + r"\s*\n+", "", body, count=1)
            self.state.sections[spec.id] = body
            return {"ok": True, "section_id": spec.id, "excerpt": body[:350]}
        except (ProjectError, httpx.HTTPError, TimeoutError, ValueError) as error:
            if not recoverable(error):
                raise
            self.state.failures[spec.id] = error_code(error)
            if isinstance(error, ProjectError) and error.code in EXHAUSTED:
                raise
            return {
                "ok": False,
                "section_id": spec.id,
                "code": error_code(error),
                "message": "本章暂未生成，请继续其他章或结束；本轮不重试。",
            }

    async def write_revision(self, request):
        state, plan = self.state, self.state.revision_plan
        if not plan or request.section_id not in plan.targets:
            raise ValueError("请先规划本次修改范围")
        if not set([*plan.targets, *plan.related]) <= set(state.read_ids):
            raise ValueError("写前须读取目标和关联原文")
        if request.section_id in state.section_specs:
            return {"ok": False, "code": "SECTION_ALREADY_ATTEMPTED"}
        original = next(b for b in state.original_blocks if b["id"] == request.section_id)
        spec = DraftSection(
            id=original["id"], title=original["title"][:80] or "文档说明", focus=request.focus
        )
        state.section_specs[spec.id] = spec
        try:
            await self.flow.event("draft_section", "正在更新相关说明")
            response = await self.runtime.model(
                REVISION_SECTION,
                {
                    **state.context(include_written=True),
                    "current_section": spec.model_dump(),
                    "original_section": original["content"],
                    "related_originals": [
                        {"id": b["id"], "content": b["content"]}
                        for b in state.original_blocks
                        if b["id"] in plan.related
                    ],
                },
                output_limit=self.flow.agent.settings.draft_section_output_tokens,
                purpose="draft_writer",
                preview_title=spec.title,
            )
            body = response.get("content")
            if not isinstance(body, str) or not body.strip() or len(body) > 24000:
                raise ProjectError("REVISION_TEXT_INVALID", "本块修改未完整生成", 503)
            if response.get("tool_calls") or body.lstrip().startswith(("{", "[")):
                raise ProjectError("REVISION_TEXT_INVALID", "本块修改格式无效", 503)
            state.sections[spec.id] = body
            return {"ok": True, "section_id": spec.id, "excerpt": body[:200]}
        except (ProjectError, httpx.HTTPError, TimeoutError, ValueError) as error:
            if not recoverable(error):
                raise
            state.failures[spec.id] = error_code(error)
            return {"ok": False, "section_id": spec.id, "code": error_code(error)}

    async def search(self, args, enabled):
        if set(args) != {"topic"} or not isinstance(args["topic"], str):
            raise ValueError("Invalid public topic")
        topic = args["topic"]
        if not enabled or not self.flow.options.get("web_search") or topic not in PUBLIC_TOPICS:
            raise ValueError("Search disabled or invalid topic")
        if topic in self.state.searched_topics or len(self.state.searched_topics) >= 2:
            return {
                "ok": False,
                "code": "SEARCH_LIMIT",
                "message": "本轮该主题已查询或查询次数已用完。",
            }
        if not (await ToolStore(self.flow.store.database).get(self.flow.principal))["enabled"]:
            return {
                "ok": False,
                "code": "SEARCH_DISABLED",
                "message": "联网已关闭，继续整理本地资料。",
            }
        self.state.searched_topics.append(topic)
        seconds = min(6, self.runtime.remaining()["seconds"])
        if seconds < 1:
            raise ProjectError("DRAFT_TIME_BUDGET", "本轮时间已用完", 503)
        refs = await search_public(
            self.flow.agent.client, PUBLIC_TOPICS[topic], request_seconds=seconds
        )
        known = {ref.id: ref for ref in self.state.references}
        for ref in refs:
            row = Reference.model_validate(ref)
            known[row.id] = row
        self.state.references = list(known.values())[-10:]
        return {
            "ok": bool(refs),
            "references": refs,
            "message": "搜索摘要仅作为建议，不是客户事实。",
        }


async def generate_prd(flow):
    from backend.agents.drafting.graph import create_draft

    flow.calls += 1
    if flow.calls > flow.agent.settings.draft_max_tools:
        raise ProjectError("TOOL_BUDGET", "本次处理较多，请分阶段继续", 503)
    flow.in_drafting = True
    try:
        result = await create_draft(flow)
        await flow.store.event(
            flow.run_id,
            {
                "type": "tool",
                "tool": "generate_prd",
                "ok": bool(result["document"]),
                "summary": "已完成需求草案整理" if result["document"] else "还需要基本业务说明",
            },
        )
        return result
    finally:
        flow.in_drafting = False

import json
import re

import httpx
from pydantic import ValidationError

from backend.agents.drafting.prompts import SECTION
from backend.agents.tools.web_search import PUBLIC_TOPIC_TOOL, PUBLIC_TOPICS, search_public
from backend.db.tool_store import ToolStore
from backend.models.consultation import Reference
from backend.models.drafting import DraftFinish, DraftPlan, DraftSection, WriteSection
from backend.models.projects import ProjectError


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
    def __init__(self, agent):
        self.agent, self.state, self.flow = agent, agent.state, agent.flow

    def schemas(self, web_enabled):
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        tools = [
            tool(
                "plan_draft",
                "组织或调整本次大纲和统一角色称呼，不修改客户事实。",
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
        from backend.agents.drafting.agent import EXHAUSTED, error_code, recoverable

        name, ok = function["name"], False
        try:
            args = json.loads(function["arguments"])
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            if name == "plan_draft":
                plan = DraftPlan.model_validate(args)
                # Already written/attempted chapters cannot silently disappear from the plan.
                known = {s.id: s for s in plan.sections}
                if any(known.get(sid) != spec for sid, spec in self.state.section_specs.items()):
                    raise ValueError("Cannot replace attempted sections")
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
                    "body": self.agent.assemble()[:12000],
                    "failed": self.state.failures,
                }
            elif name == "finish_draft":
                request = DraftFinish.model_validate(args)
                if not self.state.sections:
                    raise ValueError("No completed section; write or return full Markdown")
                if request.assumptions:
                    self.state.assumptions = request.assumptions
                self.resolve(request.resolved_questions)
                self.agent.complete(request.questions)
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
        from backend.agents.drafting.agent import EXHAUSTED, error_code, markdown, recoverable

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
            response = await self.agent.model(
                SECTION,
                {
                    **self.state.context(include_written=True),
                    "current_section": spec.model_dump(),
                    "outline": self.state.plan.model_dump() if self.state.plan else None,
                },
                output_limit=self.flow.agent.settings.draft_section_output_tokens,
            )
            body = markdown(response)
            self.state.sections[spec.id] = body
            return {"ok": True, "section_id": spec.id, "excerpt": body[:700]}
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
        seconds = min(6, self.agent.remaining()["seconds"])
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

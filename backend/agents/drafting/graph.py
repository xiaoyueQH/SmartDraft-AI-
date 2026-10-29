import asyncio
import hashlib
import re
import time

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph

from backend.agents.common import draft_revision_requested, tool_json
from backend.agents.drafting.nodes import DraftNodes
from backend.agents.drafting.state import DraftState, GraphState
from backend.db.checkpoints import DatabaseCheckpointer
from backend.db.tool_store import ToolStore
from backend.models.projects import DraftItem, ProjectError
from backend.models.review import split_markdown


def after_tools(state):
    if state["pending"]:
        return "tools"
    draft = state["draft"]
    plan = draft["plan"]
    if draft["status"] != "working" or (
        plan
        and not draft["failures"]
        and all(section["id"] in draft["sections"] for section in plan["sections"])
    ):
        return "finalize"
    return "decide"


def build_graph(nodes, checkpointer):
    graph = StateGraph(GraphState)
    graph.add_node("decide", nodes.node(nodes.decide))
    graph.add_node("tools", nodes.node(nodes.tools))
    graph.add_node("finalize", nodes.node(nodes.finalize))
    graph.add_edge(START, "decide")
    graph.add_conditional_edges("decide", lambda state: "tools" if state["pending"] else "finalize")
    graph.add_conditional_edges("tools", after_tools)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


async def run_graph(nodes, *, checkpointer=None):
    try:
        return await invoke_graph(nodes, checkpointer)
    except NodeCancelledError:
        raise asyncio.CancelledError() from None


async def invoke_graph(nodes, checkpointer):
    flow, state = nodes.flow, nodes.state
    owner = getattr(getattr(flow.principal, "account", None), "id", flow.principal)
    key = hashlib.sha256(
        tool_json(
            {
                "owner": str(owner),
                "project": str(flow.project_id),
                "run": str(flow.run_id),
                "version": flow.snapshot["project"]["version"],
                "notes": state.business_notes,
                "quotes": state.customer_statements,
                "request": state.user_request,
                "replacement": state.replacement,
                "original_blocks": state.original_blocks,
                "options": flow.options,
                "references": [ref.model_dump(mode="json") for ref in state.references],
                "previous_assumptions": [
                    item.model_dump(mode="json") for item in state.previous_assumptions
                ],
            }
        ).encode()
    ).hexdigest()
    graph = build_graph(
        nodes, checkpointer or DatabaseCheckpointer(flow.store.database, flow.project_id)
    )
    config = {
        "configurable": {"thread_id": f"draft:{flow.run_id}", "checkpoint_ns": ""},
        "recursion_limit": 300,
    }
    previous = await graph.aget_state(config)
    if previous.values:
        if previous.values["input_key"] != key:
            raise ProjectError("VERSION_CONFLICT", "起草资料已变化，请根据最新沟通重新整理", 409)
        if previous.next:
            await flow.event("draft_resume", "继续已保存的起草进度")
            result = await graph.ainvoke(None, config=config, durability="sync")
        else:
            result = previous.values
    else:
        web_enabled = bool(
            flow.options.get("web_search")
            and (await ToolStore(flow.store.database).get(flow.principal))["enabled"]
        )
        result = await graph.ainvoke(
            {
                "input_key": key,
                "draft": state.model_dump(mode="json"),
                "pending": [],
                "counters": {
                    name: getattr(flow, name, 0) for name in ("rounds", "tokens", "calls")
                },
                "expires_at": time.time()
                + max(0, nodes.deadline - asyncio.get_running_loop().time()),
                "web_enabled": web_enabled,
                "body": "",
                "partial": False,
            },
            config=config,
            durability="sync",
        )
    nodes.restore(result)
    return result["body"], result["partial"]


LABELS = {
    "business_background": "业务背景",
    "current_workflow": "当前业务流程",
    "problems": "现有痛点",
    "users": "参与角色与职责",
    "goals": "建设目标",
    "scope": "本期范围",
    "constraints": "约束与排除范围",
    "technology_preference": "已说明的技术偏好",
}


def saved_notes(notes):
    lines = ["## 客户已说明的信息", ""]
    for key, label in LABELS.items():
        value = notes.get(key)
        if not value or value == "待确认":
            continue
        lines += ["### " + label]
        lines += ["- " + item for item in value] if isinstance(value, list) else [value]
    return "\n".join(lines)


def label_proposals(body):
    return body.replace("【已说明】", "").replace("【建议，待确认】", "【自动生成】")


def pending_questions(body, notes, state, resolved=(), *, include_open=True):
    section = re.search(r"^##\s*待确认事项\s*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    questions = []
    if section:
        questions = re.findall(r"^\s*(?:[-*]|\d+[.、])\s*(.+)$", section[1], re.M)
    if include_open:
        questions += [q for q in notes.get("open_questions", []) if q not in resolved]
    questions += [gap.question for gap in state.conflicts]
    return list(
        dict.fromkeys(q.strip() for q in questions if q.strip() not in {"暂无", "无", "暂无。"})
    )


async def create_draft(flow, *, checkpointer=None):
    notes = flow.notes
    # A short read rechecks ownership/version; it does not re-extract history with a model.
    rows = await flow.store.draft_history(
        flow.principal,
        flow.project_id,
        flow.snapshot["project"]["version"],
        flow.agent.settings.draft_history_chars,
    )
    quotes = list(
        reversed(dict.fromkeys(row["content"] for row in reversed(rows) if row["role"] == "user"))
    )
    if not notes and not quotes:
        raise ProjectError(
            "BUSINESS_CONTEXT_REQUIRED", "还没有可整理的业务信息，请先简单说说想做什么", 422
        )
    if not notes and not any(
        row["role"] == "user" and row.get("run_id") != str(flow.run_id) for row in rows
    ):
        raise ProjectError(
            "BUSINESS_CONTEXT_REQUIRED", "还没有已保存的业务信息，请先说说想解决什么问题", 422
        )
    draft_state = DraftState(
        business_notes=notes,
        user_request=getattr(flow, "prompt", "请根据已存资料起草需求书"),
        customer_statements=quotes,
        references=[ref.model_copy(deep=True) for ref in flow.state.references],
        previous_assumptions=[
            item.model_copy(deep=True)
            for item in flow.state.draft_assumptions
            if not (
                notes.get("technology_preference")
                and notes["technology_preference"] not in {"待确认", "未知", "未说明"}
            )
        ],
    ).model_copy(deep=True)
    if draft_revision_requested(draft_state.user_request):
        original = await flow.store.draft_target(
            flow.principal, flow.project_id, flow.snapshot["project"]["version"]
        )
        if original is None:
            raise ProjectError(
                "DRAFT_REVISION_MISSING", "没有可以补写的草稿；正式版本请通过需求变更处理", 409
            )
        draft_state.original_blocks = split_markdown(original["content"])
        for block in draft_state.original_blocks:
            block["id"] = f"existing-{block['position'] + 1}"
        draft_state.original_questions = list(original["questions"])
        draft_state.replacement = {
            "id": original["id"],
            "version": original["version"],
            "title": original["title"],
        }
    nodes = DraftNodes(flow, draft_state)
    body, partial = await run_graph(nodes, checkpointer=checkpointer)
    if (
        partial
        and not draft_state.original_blocks
        and await flow.store.draft_target(
            flow.principal, flow.project_id, flow.snapshot["project"]["version"]
        )
    ):
        raise ProjectError("DRAFT_REGEN_INCOMPLETE", "本次重新生成未完成，最新草案保持不变", 503)
    flow.state.references = draft_state.references
    if draft_state.assumptions and (draft_state.sections or draft_state.direct_body):
        flow.state.draft_assumptions = draft_state.assumptions
    if draft_state.original_blocks:
        if not draft_state.sections:
            raise ProjectError("DRAFT_REVISION_EMPTY", "没有完成章节补写，原稿保持不变", 503)
        revised_titles = [
            b["title"] for b in draft_state.original_blocks if b["id"] in draft_state.sections
        ]
        questions = [
            q
            for q in draft_state.original_questions
            if not any(title in q and "章节尚需补充细节" in q for title in revised_titles)
        ]
        questions = list(dict.fromkeys([*questions, *draft_state.questions]))
        for title in revised_titles:
            body = body.replace(f"- “{title}”章节尚需补充细节。", "")
        if "本章暂未生成" not in body:
            questions = [q for q in questions if q != "详细功能、异常处理和验收方式尚需完善。"]
            body = body.replace("- 详细功能、异常处理和验收方式尚需完善。", "")
        body = re.sub(r"(^## 待确认事项\s*\n)\s*(?=^## |\Z)", r"\1\n暂无\n\n", body, flags=re.M)
        partial = partial or "本章暂未生成" in body
        document = DraftItem(
            kind="specification",
            title=draft_state.replacement["title"],
            content=body,
            questions=questions,
        )
        await flow.event("draft_complete", "已补写指定章节，其他内容保持不变")
        return {
            "document": document.model_dump(),
            "partial": partial,
            "replacement": draft_state.replacement,
        }
    body = (
        "本草案根据客户要求和自动补充方案整理，尚未立项。"
        "用户未明确选择的设计属于【自动生成】方案，可调整，不代表用户已选择或确认；"
        "用户原始要求保留在文末，后续以用户最新要求为准。\n\n" + label_proposals(body)
    )
    if draft_state.assumptions:
        body += "\n\n## 自动补充方案（可调整）\n\n" + "\n\n".join(
            f"### {item.topic}\n\n【自动生成】{item.value}"
            + (f"\n\n选择依据：{item.rationale}" if item.rationale else "")
            for item in draft_state.assumptions
        )
    if notes:
        body += "\n\n" + saved_notes(draft_state.context()["business_notes"])
    elif quotes:
        body += "\n\n## 客户原话\n\n" + "\n\n".join(quotes)
    questions = pending_questions(
        body, notes, flow.state, draft_state.resolved_questions, include_open=partial
    )
    if partial:
        questions.append("详细功能、异常处理和验收方式尚需完善。")
    await flow.event("draft_complete", "已汇总草案和待确认事项")
    if len(questions) > 100 or len(body) > 120000:
        raise ProjectError("DRAFT_CAPACITY", "草案超过单次容量，请分模块整理；原有信息已保留", 503)
    title = notes.get("subject") or flow.snapshot["project"]["title"]
    document = DraftItem(
        kind="specification", title=title[:150] + "需求草案", content=body, questions=questions
    )
    return {"document": document.model_dump(), "partial": partial}

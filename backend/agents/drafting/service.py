import re

from backend.agents.drafting.agent import DraftAgent
from backend.agents.drafting.state import DraftState
from backend.models.projects import DraftItem, ProjectError

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
    return body.replace("【已说明】", "").replace("【建议，待确认】", "【自动补充，可调整】")


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


async def create_draft(flow):
    notes = flow.notes
    # A short read rechecks ownership/version; it does not re-extract history with a model.
    rows = await flow.store.draft_history(
        flow.principal,
        flow.project_id,
        flow.snapshot["project"]["version"],
        flow.agent.settings.draft_history_chars,
    )
    quotes = list(dict.fromkeys(source.quote for source in flow.state.sources if source.quote))
    if not notes or flow.state.memory_pending:
        quotes = list(dict.fromkeys(row["content"] for row in rows if row["role"] == "user"))
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
    writer = DraftAgent(flow, draft_state)
    body, partial = await writer.run()
    flow.state.references = draft_state.references
    if draft_state.assumptions and (draft_state.sections or draft_state.direct_body):
        flow.state.draft_assumptions = draft_state.assumptions
    body = (
        "本草案根据客户要求和自动补充方案整理，尚未立项。"
        "未说明的细节已按可实施方案补齐，后续以客户最新要求为准；原始要求保留在文末。\n\n"
        + label_proposals(body)
    )
    if draft_state.assumptions:
        body += "\n\n## 自动补充方案（可调整）\n\n" + "\n\n".join(
            f"### {item.topic}\n\n{item.value}"
            + (f"\n\n选择依据：{item.rationale}" if item.rationale else "")
            for item in draft_state.assumptions
        )
    if notes:
        body += "\n\n" + saved_notes(notes)
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

import difflib
from uuid import UUID

from backend.models.projects import DiscoveryUpdate, DraftItem, ProjectError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember_requirements",
            "description": (
                "记录客户明确提供或确认的业务事实和待确认问题，随本轮对话保存。"
                "只提交新增事实，列表会累积。resolved_questions标记已解决的原问题，"
                "remove_facts仅用于客户明确纠正的原事实。不代表已确认需求或立项。"
            ),
            "parameters": DiscoveryUpdate.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project",
            "description": "读取当前项目、已确认需求基线和最近的草稿工单。所有引用使用实际资料ID。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "按ID读取本项目的完整历史资料。",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "string"}},
                "required": ["item_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_baseline",
            "description": "比较拟定完整需求正文和当前基线的文字差异，不代表代码影响分析。",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "maxLength": 120000}},
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_draft",
            "description": (
                "准备待确认草稿，不发布基线或关闭工单。"
                "change正文包含变更说明及变更后的完整需求书。"
                "task/acceptance仅负责人可创建。与最终回答成功后一并保存。"
            ),
            "parameters": DraftItem.model_json_schema(),
        },
    },
]


class ProjectTools:
    def __init__(self, store, principal, project_id):
        self.store, self.principal, self.project_id = store, principal, project_id
        self.drafts: list[dict] = []
        self.notes: list[dict] = []

    async def call(self, name, arguments, *, state=None):
        if state is not None:
            self.drafts, self.notes = state["drafts"], state["notes"]

        handlers = {
            "remember_requirements": remember,
            "save_draft": prepare,
            "read_project": read_project,
            "read_document": read_document,
            "compare_baseline": compare_baseline,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError("未知工具")
        return await handler(self, arguments)


async def remember(context, arguments):
    notes = DiscoveryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
    context.notes.append(notes)
    return {"prepared": True, "updated_fields": list(notes), "message": "沟通纪要随本轮成功后保存"}


async def prepare(context, arguments):
    draft = DraftItem.model_validate(arguments)
    if draft.kind in {"task", "acceptance"} and context.principal.account.role != "admin":
        raise ProjectError("FORBIDDEN", "内部任务与交付记录由负责人创建", 403)
    snapshot = await context.store.detail(context.principal, context.project_id, summaries=True)
    if draft.kind != "specification" and not snapshot["project"]["revision"]:
        raise ProjectError("BASELINE_REQUIRED", "请先完成初版需求书确认")
    if len(context.drafts) >= 3:
        raise ValueError("每轮最多准备三份草稿")
    context.drafts.append(draft.model_dump())
    return {
        "prepared": True,
        "title": draft.title,
        "kind": draft.kind,
        "requires_confirmation": True,
        "message": "草稿随本轮成功后保存，尚未提交或发布",
    }


async def read_project(context, arguments):
    snapshot = await context.store.detail(context.principal, context.project_id, summaries=True)
    return {
        "project": snapshot["project"],
        "baseline": snapshot["baseline"],
        "recent_items": [
            {k: row[k] for k in ("id", "kind", "title", "status", "base_revision", "questions")}
            for row in snapshot["items"][:15]
        ],
    }


async def read_document(context, arguments):
    return await context.store.item(
        context.principal, context.project_id, UUID(arguments["item_id"])
    )


async def compare_baseline(context, arguments):
    content = arguments.get("content", "")
    if not isinstance(content, str) or len(content) > 120000:
        raise ValueError("正文超出限制")
    snapshot = await context.store.detail(context.principal, context.project_id, summaries=True)
    baseline = (
        await context.store.item(
            context.principal, context.project_id, UUID(snapshot["baseline"]["id"])
        )
        if snapshot["baseline"]
        else None
    )
    original = baseline["content"] if baseline else ""
    difference = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            content.splitlines(),
            fromfile="已确认基线",
            tofile="拟变更正文",
            lineterm="",
        )
    )
    return {"base_revision": snapshot["project"]["revision"], "diff": difference[:16000]}

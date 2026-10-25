import difflib
import json

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
                "properties": {"content": {"type": "string", "maxLength": 24000}},
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

    async def call(self, name, arguments):
        if name == "remember_requirements":
            notes = DiscoveryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
            self.notes.append(notes)
            return {
                "prepared": True,
                "updated_fields": list(notes),
                "message": "已整理沟通纪要，随本轮成功后保存；不代表正式需求确认",
            }
        if name == "read_project":
            snapshot = await self.store.detail(self.principal, self.project_id)
            return {
                "project": snapshot["project"],
                "baseline": snapshot["baseline"],
                "recent_items": [
                    {
                        k: row[k]
                        for k in ("id", "kind", "title", "status", "base_revision", "questions")
                    }
                    for row in snapshot["items"][:15]
                ],
            }
        if name == "read_document":
            from uuid import UUID

            return await self.store.item(
                self.principal, self.project_id, UUID(arguments["item_id"])
            )
        if name == "compare_baseline":
            content = arguments.get("content", "")
            if not isinstance(content, str) or len(content) > 24000:
                raise ValueError("正文超出限制")
            snapshot = await self.store.detail(self.principal, self.project_id)
            original = snapshot["baseline"]["content"] if snapshot["baseline"] else ""
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
        if name == "save_draft":
            draft = DraftItem.model_validate(arguments)
            if draft.kind in {"task", "acceptance"} and self.principal.account.role != "admin":
                raise ProjectError("FORBIDDEN", "内部任务与交付记录由负责人创建", 403)
            snapshot = await self.store.detail(self.principal, self.project_id)
            if draft.kind != "specification" and not snapshot["project"]["revision"]:
                raise ProjectError("BASELINE_REQUIRED", "请先完成初版需求书确认")
            if len(self.drafts) >= 3:
                raise ValueError("每轮最多准备三份草稿")
            self.drafts.append(draft.model_dump())
            return {
                "prepared": True,
                "title": draft.title,
                "kind": draft.kind,
                "requires_confirmation": True,
                "message": "已准备草稿，将随本轮对话原子保存；尚未提交评审或执行业务",
            }
        raise ValueError("未知工具")


def tool_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)

import json

from backend.models.projects import DiscoveryUpdate, DraftItem

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

    async def call(self, name, arguments):
        from backend.agents.tools.discovery import remember
        from backend.agents.tools.drafts import prepare
        from backend.agents.tools.project import compare_baseline, read_document, read_project

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


def tool_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)

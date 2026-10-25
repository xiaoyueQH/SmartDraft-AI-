import json

FULL_NOTES = {
    "business_background": "维修预约",
    "current_workflow": "前台接电话登记纸本",
    "users": ["前台", "店员"],
    "goals": ["减少漏单"],
    "scope": ["登记和查询预约"],
    "technology_preference": "TypeScript、Go 或现有平台，待技术评审",
}


def extracted(prompt, notes=None, **extra):
    notes = notes or {}
    sources = []
    for field, value in notes.items():
        if field == "open_questions":
            continue
        for item in value if isinstance(value, list) else [value]:
            sources.append({"field": field, "value": item, "quote": prompt})
    return {
        "content": json.dumps(
            {"notes": notes, "sources": sources, "reply": "已了解", **extra}, ensure_ascii=False
        )
    }, {"total_tokens": 100}


class DraftModel:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.web_calls = []
        self.fail_write = False
        self.bad_basis = False
        self.notes = FULL_NOTES
        self.research = False
        self.request_quote = ""
        self.pause_quote = ""
        self.change = False

    async def respond(self, messages):
        task = messages[0]["content"]
        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        self.calls.append(task.split("。", 1)[0])
        if "CONSULTATION_ADVISE" in task:
            response, _ = extracted(data["input"], self.notes)
            result = json.loads(response["content"])
            result["reply"] = "你们可以先把接单和发货串起来，权限按实际分工区分。"
            if self.pause_quote and self.pause_quote in data["input"]:
                result["reply"] = "好的，暂时不整理，我们先讨论你最关心的地方。"
            result["decision"] = {
                "ready": self.ready,
                "request_quote": self.request_quote,
                "pause_quote": self.pause_quote,
                "material_change": self.change,
            }
            return {
                "tool_calls": [
                    {
                        "id": "record",
                        "type": "function",
                        "function": {
                            "name": "record_consultation",
                            "arguments": json.dumps(result, ensure_ascii=False),
                        },
                    }
                ]
            }, {"total_tokens": 100}
        elif "CONSULTATION_REPLY" in task:
            result = None
            return {"content": "我们可以先从最费时间的一步讨论，不必现在决定所有细节。"}, {
                "total_tokens": 100
            }
        elif "DRAFT_WRITE" in task:
            if self.fail_write:
                raise TimeoutError("private provider failure")
            return {
                "content": "# 业务需求草案\n\n## 业务方案\n"
                + "根据已保存信息组织登记、核对和发货流程，角色权限遵循已说明分工。" * 4
                + "\n\n## 待确认事项\n- 新系统的数据迁移方式仍需确认。"
            }, {"total_tokens": 100}
        else:
            raise AssertionError(task[:100])

import json

from backend.models.review import ChangesInput, FindingsInput


def schema(name, description, parameters):
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def finish_schema():
    findings, changes = FindingsInput.model_json_schema(), ChangesInput.model_json_schema()
    return {
        "type": "object",
        "$defs": {**findings.get("$defs", {}), **changes.get("$defs", {})},
        "properties": {
            "findings": findings["properties"]["findings"],
            "changes": {"type": "array", "items": {"$ref": "#/$defs/BlockChange"}, "maxItems": 12},
            "summary": {"type": "string", "maxLength": 1200},
        },
        "additionalProperties": False,
    }


TOOLS = [
    schema(
        "read_customer_sources",
        "按来源编号读取客户原话，或按原文关键词查找历史。每批最多4条；offset用于继续读取。",
        {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "query": {"type": "string", "maxLength": 120},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
            },
            "additionalProperties": False,
        },
    ),
    schema(
        "read_blocks",
        "读取相关Markdown块的完整原文，每批最多4块、合计24000字符。",
        {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}
            },
            "required": ["ids"],
            "additionalProperties": False,
        },
    ),
    schema(
        "record_findings",
        "记录有原文依据的问题；同一问题可关联多个块。",
        FindingsInput.model_json_schema(),
    ),
    schema(
        "propose_changes",
        "针对已有问题提交精确原文替换，服务器生成候选稿，原稿不变。",
        ChangesInput.model_json_schema(),
    ),
    schema(
        "finish_review",
        "提交最终检查结论并结束，可一并提交有依据的问题及精确修改；未读章节自动标明。",
        finish_schema(),
    ),
]


def assemble(blocks, changes):
    pieces = []
    for block in blocks:
        edits = [change for change in changes if change["block_id"] == block["id"]]
        body = block["content"]
        for edit in sorted(edits, key=lambda change: change["start"], reverse=True):
            body = body[: edit["start"]] + edit["new_text"] + body[edit["end"] :]
        pieces.append(body)
    return "".join(pieces)


def client_strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in client_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in client_strings(item)]
    return []


class ReviewTools:
    def __init__(self, source):
        self.client_texts = client_strings(source["client_quotes"])

    def call(self, function, state):
        try:
            args = json.loads(function["arguments"])
            name = function["name"]
            by_id = {block["id"]: block for block in state["blocks"]}
            read = set(state["read_ids"])
            if name == "read_blocks":
                if (
                    set(args) != {"ids"}
                    or not isinstance(args["ids"], list)
                    or not 1 <= len(args["ids"]) <= 4
                ):
                    return {
                        "ok": False,
                        "code": "REVIEW_BLOCK_SELECTION",
                        "message": (
                            "每次只能读取1至4个块ID；超过4个请拆成多次read_blocks调用，"
                            "不要重复本次参数。"
                        ),
                    }
                rows = [by_id[sid] for sid in dict.fromkeys(args["ids"])]
                if sum(len(row["content"]) for row in rows) > 24000:
                    return {"ok": False, "message": "本批内容较长，请减少读取块数。"}
                state["read_ids"] = list(dict.fromkeys([*state["read_ids"], *args["ids"]]))
                return {"ok": True, "blocks": rows}
            if name == "record_findings":
                findings = [f.model_dump() for f in FindingsInput.model_validate(args).findings]
                existing = {f["id"]: f for f in state["findings"]}
                accepted, rejected = [], []
                for finding in findings:
                    targets = finding["block_ids"]
                    reason = ""
                    if not set(targets) <= read or not any(
                        finding["evidence"] in by_id[sid]["content"] for sid in targets
                    ):
                        reason = (
                            "依据须为已读目标块的一段连续原文，不能拼接、补代码围栏或省略中间行。"
                        )
                    elif finding["kind"] == "client_mismatch" and (
                        not finding["source_quote"]
                        or not any(finding["source_quote"] in text for text in self.client_texts)
                    ):
                        reason = (
                            "source_quote须逐字引用customer_statements中的客户原话，"
                            "不能包含草案标题、纪要标题或自动方案。无法找到原话时使用suggestion，"
                            "不要重复提交相同参数。"
                        )
                    elif finding["kind"] == "decision" and not finding["question"]:
                        reason = "业务取舍须填写需要客户确认的问题。"
                    elif finding["kind"] != "decision" and finding["question"]:
                        reason = "只有业务取舍才填写question。"
                    elif finding["id"] in existing and existing[finding["id"]] != finding:
                        reason = "该问题ID已记录不同内容，请只提交尚未记录的问题。"
                    elif finding["id"] not in existing and len(existing) >= 30:
                        reason = "本次问题数量已达到上限。"
                    if reason:
                        rejected.append({"id": finding["id"], "message": reason})
                        continue
                    existing[finding["id"]] = finding
                    accepted.append(finding["id"])
                state["findings"] = list(existing.values())
                return {
                    "ok": bool(accepted),
                    "count": len(accepted),
                    "accepted_ids": accepted,
                    "rejected": rejected,
                    "message": "有效问题已保留；请根据rejected逐条修正依据，不要重复原参数。"
                    if accepted
                    else "本批没有有效问题，请检查rejected中的具体原因；不要重复原参数。",
                }
            if name == "propose_changes":
                changes = [c.model_dump() for c in ChangesInput.model_validate(args).changes]
                findings = {f["id"]: f for f in state["findings"]}
                pending = list(state["changes"])
                for change in changes:
                    sid = change["block_id"]
                    block = by_id[sid]
                    if sid not in read or block["content"].count(change["old_text"]) != 1:
                        raise ValueError("Ambiguous source span")
                    linked = [findings[fid] for fid in change["finding_ids"]]
                    if any(
                        f["kind"] not in {"technical", "client_mismatch"}
                        or sid not in f["block_ids"]
                        for f in linked
                    ):
                        raise ValueError("Change needs an actionable finding")
                    if (
                        "【自动生成】" in change["old_text"]
                        and "【自动生成】" not in change["new_text"]
                        and not all(f["kind"] == "client_mismatch" for f in linked)
                    ):
                        raise ValueError("Keep provenance marker")
                    if change["old_text"] == change["new_text"]:
                        raise ValueError("Empty change")
                    start = block["content"].index(change["old_text"])
                    change.update(start=start, end=start + len(change["old_text"]))
                    if any(
                        old["block_id"] == sid
                        and not (change["end"] <= old["start"] or change["start"] >= old["end"])
                        for old in pending
                    ):
                        raise ValueError("Overlapping changes")
                    pending.append(change)
                if len(pending) > 30 or len(assemble(state["blocks"], pending)) > 120000:
                    raise ValueError("Candidate size exceeded")
                state["changes"] = pending
                return {"ok": True, "count": len(changes), "message": "候选修改已记录，尚未应用。"}
            if (
                name == "finish_review"
                and isinstance(args, dict)
                and set(args) <= {"findings", "changes", "summary"}
            ):
                results = []
                if args.get("findings"):
                    results.append(
                        self.call(
                            {
                                "name": "record_findings",
                                "arguments": json.dumps({"findings": args["findings"]}),
                            },
                            state,
                        )
                    )
                if args.get("changes"):
                    results.append(
                        self.call(
                            {
                                "name": "propose_changes",
                                "arguments": json.dumps({"changes": args["changes"]}),
                            },
                            state,
                        )
                    )
                if any(not r["ok"] or r.get("rejected") for r in results):
                    state["partial"] = True
                if isinstance(args.get("summary"), str) and len(args["summary"]) <= 1200:
                    state["summary"] = args["summary"]
                state["complete"] = True
                return {"ok": all(r["ok"] for r in results), "results": results}
            raise ValueError("Unknown tool")
        except (ValueError, KeyError, TypeError) as error:
            reasons = {
                "Unverified evidence": "请先读取目标块，并引用其中连续原文。",
                "Client evidence required": "客户要求修正需要提供真实客户原话source_quote。",
                "Ambiguous source span": "old_text须与已读块原文完全一致且只出现一次，包括换行。",
                "Change needs an actionable finding": (
                    "修改须关联目标块上已记录的技术错误或客户要求冲突。"
                ),
                "Keep provenance marker": (
                    "自动设计须保留【自动生成】；按客户明确要求修正可使用client_mismatch并附原话。"
                ),
                "Overlapping changes": "这段原文已在候选修改中，请勿重复或交叉修改。",
            }
            return {
                "ok": False,
                "code": "REVIEW_TOOL_INVALID",
                "message": reasons.get(
                    str(error), "参数或原文依据不匹配，请核对块ID和原文；原稿未改变。"
                ),
            }

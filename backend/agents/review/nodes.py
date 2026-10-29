import hashlib
import json
import time
from time import monotonic

from langgraph.types import interrupt

from backend.agents.common import measured_tokens, request_budget, technology_has_source, tool_json
from backend.agents.review.prompts import SYSTEM, VERIFY
from backend.agents.tools.review import TOOLS, ReviewTools, assemble
from backend.models.projects import ProjectError
from backend.models.review import ApplyReview, Verification, split_markdown


class ReviewNodes:
    def __init__(self, runtime, store, principal, project_id, item_id, review_id, job):
        self.runtime, self.store, self.principal = runtime, store, principal
        self.project_id, self.item_id, self.review_id = project_id, item_id, review_id
        self.job = job
        self.tools = ReviewTools(job["source"])

    def source_facts(self, state=None):
        facts = dict(self.job["source"]["facts"])
        unverified = {}
        choice = facts.get("technology_preference")
        quotes = [
            *self.job["source"]["client_quotes"],
            *(row["content"] for row in (state or {}).get("loaded_sources", [])),
        ]
        if choice and not technology_has_source(choice, quotes):
            unverified["technology_preference"] = facts.pop("technology_preference")
        return facts, unverified

    async def split_blocks(self, state):
        blocks = split_markdown(self.job["source"]["content"], self.job["blocks"])
        if len(blocks) > 100:
            raise ProjectError("REVIEW_BLOCK_LIMIT", "文档章节较多，请分阶段检查", 503)
        await self.store.save_blocks(self.review_id, blocks)
        await self.store.event(self.review_id, "已按章节拆分，正在核对文档内容", type="node")
        return {"blocks": blocks}

    async def model(self, state, messages, tools=None, json_output=False, tool_choice=None):
        settings = self.runtime.settings
        remaining_time = state["expires_at"] - time.time()
        if state["rounds"] >= settings.review_max_rounds or remaining_time <= 1:
            raise ProjectError("REVIEW_BUDGET", "本次检查达到预算，已保留完成内容", 503)
        reserve, allowance = request_budget(
            messages,
            bool(tools),
            settings.review_token_budget - state["tokens"],
            tools=tools,
            max_output=2400 if tools else 1200,
        )
        step = monotonic()
        state["rounds"] += 1
        message, usage = await self.runtime.complete(
            messages,
            allow_tools=bool(tools),
            tools=tools,
            json_output=json_output,
            purpose="draft_reviewer",
            max_output_tokens=allowance,
            timeout_seconds=min(remaining_time, 45),
            tool_choice=tool_choice,
            max_attempts=min(
                2, settings.review_max_rounds - state["rounds"] + 1 - int(bool(tools))
            ),
        )
        state["rounds"] += max(0, usage.get("attempts", 1) - 1)
        used = measured_tokens(usage, reserve + allowance)
        state["tokens"] += used
        await self.store.event(
            self.review_id,
            "已完成一次文档核对",
            type="model",
            model=usage.get("model"),
            purpose="draft_reviewer",
            attempts=usage.get("attempts", 1),
            fallback_from=usage.get("fallback_from"),
            duration_ms=round((monotonic() - step) * 1000),
            tokens=used,
        )
        if state["tokens"] > settings.review_token_budget:
            raise ProjectError("REVIEW_BUDGET", "本次检查达到预算，已保留完成内容", 503)
        return message

    async def assess(self, state):
        if state["rounds"] >= self.runtime.settings.review_max_rounds - 1:
            return {
                **state,
                "pending": [],
                "partial": True,
                "stop_reasons": [
                    *state.get("stop_reasons", []),
                    "本次检查次数已用完，仍有内容未检查完成。",
                ],
            }
        manifest = [
            {
                "id": b["id"],
                "title": b["title"],
                "characters": len(b["content"]),
                "preview": b["content"][:60],
            }
            for b in state["blocks"]
        ]
        remaining = 6000
        for block, entry in zip(state["blocks"], manifest, strict=True):
            size = len(block["content"])
            if size <= min(1200, remaining):
                entry["content"] = block["content"]
                entry.pop("preview", None)
                if block["id"] not in state["read_ids"]:
                    state["read_ids"].append(block["id"])
                remaining -= size
        facts, unverified = self.source_facts(state)
        source_manifest = self.job["source"].get("source_manifest", [])
        unread_sources = [
            row for row in source_manifest if row["id"] not in state.get("read_source_ids", [])
        ]
        data = {
            "manifest": manifest,
            "customer_facts": facts,
            "unverified_historical_notes": unverified,
            "customer_statements": self.job["source"]["client_quotes"],
            "customer_source_directory": unread_sources[:20],
            "remaining_customer_sources": len(unread_sources),
            "loaded_customer_sources": state.get("loaded_sources", [])[-4:],
            "automatic_designs": self.job["source"]["assumptions"],
            "read_ids": state["read_ids"],
            "findings": state["findings"],
            "proposed_blocks": list(dict.fromkeys(c["block_id"] for c in state["changes"])),
            "remaining_rounds": self.runtime.settings.review_max_rounds - state["rounds"],
            "remaining_tools": self.runtime.settings.review_max_tools - state["calls"],
        }
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": tool_json(data)},
            *state["messages"],
        ]
        try:
            message = await self.model(state, messages, TOOLS)
            calls = message.get("tool_calls") or []
            if not calls and state["rounds"] < self.runtime.settings.review_max_rounds - 1:
                message = await self.model(
                    state,
                    [
                        *messages,
                        {"role": "assistant", "content": (message.get("content") or "")[:5000]},
                        {
                            "role": "user",
                            "content": (
                                "请通过finish_review提交本次检查结论。"
                                "findings只写有原文依据的问题，changes仅提交精确补丁；"
                                "没有问题或补丁可填空列表。"
                                "客户原话须引用customer_statements，不引用草案标题。"
                                "不要只回复解释文字。"
                            ),
                        },
                    ],
                    TOOLS,
                    tool_choice={"type": "function", "function": {"name": "finish_review"}},
                )
                calls = message.get("tool_calls") or []
            if not calls:
                state["partial"] = True
                state.setdefault("stop_reasons", []).append(
                    "检查助手未提交有效检查结论，不能视为没有问题。"
                )
            return {
                **state,
                "pending": calls[:24],
                "messages": [{"role": "assistant", "content": "", "tool_calls": calls[:24]}]
                if calls
                else [],
            }
        except (ProjectError, TimeoutError) as error:
            reason = (
                error.message
                if isinstance(error, ProjectError)
                else "检查请求超时，已保留完成内容。"
            )
            await self.store.event(
                self.review_id, reason, type="error", code=getattr(error, "code", "TIMEOUT")
            )
            return {
                **state,
                "pending": [],
                "partial": True,
                "stop_reasons": [*state.get("stop_reasons", []), reason],
            }

    async def act(self, state):
        call, *pending = state["pending"]
        state["calls"] += 1
        if state["calls"] > self.runtime.settings.review_max_tools:
            state.update(partial=True, complete=True, pending=[])
            return state
        if call["function"]["name"] == "read_customer_sources":
            try:
                args = json.loads(call["function"]["arguments"])
                if not isinstance(args, dict) or not set(args) <= {"ids", "query", "offset"}:
                    raise ValueError("来源查询参数不正确。")
                ids, query, offset = (
                    args.get("ids", []),
                    args.get("query", ""),
                    args.get("offset", 0),
                )
                if (
                    not isinstance(ids, list)
                    or any(not isinstance(sid, str) for sid in ids)
                    or not isinstance(query, str)
                    or len(query) > 120
                    or type(offset) is not int
                    or not 0 <= offset <= 10000
                ):
                    raise ValueError("来源查询参数不正确。")
                result = await self.store.read_sources(
                    self.principal,
                    self.project_id,
                    self.item_id,
                    self.review_id,
                    ids=ids,
                    query=query,
                    offset=offset,
                )
                loaded = {row["id"]: row for row in state.get("loaded_sources", [])}
                loaded.update({row["id"]: row for row in result["sources"]})
                if sum(len(row["content"]) for row in loaded.values()) > 60000:
                    raise ValueError("本次读取原话达到预算，请分阶段继续核对。")
                state["loaded_sources"] = list(loaded.values())
                state["read_source_ids"] = list(
                    dict.fromkeys(
                        [
                            *state.get("read_source_ids", []),
                            *(row["id"] for row in result["sources"]),
                        ]
                    )
                )
                result["ok"] = True
            except (ValueError, TypeError) as error:
                result = {"ok": False, "message": str(error)}
        else:
            self.tools.client_texts = [
                *self.job["source"]["client_quotes"],
                *(row["content"] for row in state.get("loaded_sources", [])),
            ]
            result = self.tools.call(call["function"], state)
        signature = hashlib.sha256(tool_json(call["function"]).encode()).hexdigest()
        if not result["ok"]:
            state.setdefault("stop_reasons", []).append(
                "检查建议引用的内容不是客户原话，该建议未采用。"
                if any(
                    "source_quote" in row.get("message", "") for row in result.get("rejected", [])
                )
                else "检查建议未通过原文依据核对，该建议未采用；原稿保持不变。"
            )
            if state.get("last_invalid_call") == signature:
                state.update(partial=True, complete=True)
                state["stop_reasons"].append("相同检查请求连续失败，已停止重复尝试，请保留原稿。")
            state["last_invalid_call"] = signature
        else:
            state["last_invalid_call"] = ""
        await self.store.event(
            self.review_id,
            "已读取相关章节" if call["function"]["name"] == "read_blocks" else "已处理本次检查建议",
            type="tool",
            tool=call["function"]["name"],
            ok=result["ok"],
            code=result.get("code", "OK" if result["ok"] else "REVIEW_TOOL_INVALID"),
        )
        state["messages"] = [
            *state["messages"],
            {"role": "tool", "tool_call_id": call["id"], "content": tool_json(result)},
        ]
        state["pending"] = pending
        if state["complete"]:
            state["pending"] = []
        return state

    async def verify(self, state):
        state["verified"] = not state["changes"]
        if state["changes"]:
            ids = {change["block_id"] for change in state["changes"]}
            finding_ids = {fid for change in state["changes"] for fid in change["finding_ids"]}
            related = {
                sid
                for finding in state["findings"]
                if ids.intersection(finding["block_ids"])
                for sid in finding["block_ids"]
            }
            changed = []
            for block in state["blocks"]:
                if block["id"] in ids | related:
                    changed.append(
                        {
                            "id": block["id"],
                            "before": block["content"],
                            "after": assemble([block], state["changes"]),
                        }
                    )
            try:
                message = await self.model(
                    state,
                    [
                        {"role": "system", "content": VERIFY},
                        {
                            "role": "user",
                            "content": tool_json(
                                {
                                    "changed_blocks": changed,
                                    "findings": [
                                        f for f in state["findings"] if f["id"] in finding_ids
                                    ],
                                    "unmodified_findings": [
                                        f for f in state["findings"] if f["id"] not in finding_ids
                                    ],
                                    "customer_facts": self.source_facts(state)[0],
                                    "customer_statements": [
                                        f["source_quote"]
                                        for f in state["findings"]
                                        if f["id"] in finding_ids and f.get("source_quote")
                                    ],
                                }
                            ),
                        },
                    ],
                    json_output=True,
                )
                result = Verification.model_validate_json(message.get("content") or "")
                state["verified"] = result.passed and not result.unresolved_ids
                state["summary"] = result.summary
            except (ProjectError, TimeoutError, ValueError):
                state["verified"], state["partial"] = False, True
        return state

    async def finalize(self, state):
        unread = [b["id"] for b in state["blocks"] if b["id"] not in state["read_ids"]]
        patched = {fid for change in state["changes"] for fid in change["finding_ids"]}
        unfixed = [
            f["id"]
            for f in state["findings"]
            if f["kind"] in {"technical", "client_mismatch"}
            and (f["id"] not in patched or not state["verified"])
        ]
        sources = self.job["source"].get("source_manifest", [])
        unread_sources = [
            row["id"] for row in sources if row["id"] not in state.get("read_source_ids", [])
        ]
        report = {
            "findings": state["findings"],
            "changes": state["changes"],
            "questions": [f["question"] for f in state["findings"] if f["kind"] == "decision"],
            "verified": state["verified"],
            "partial": state["partial"]
            or bool(unread)
            or bool(unfixed)
            or bool(unread_sources)
            or self.job["source"].get("source_overflow", False),
            "checked_sources": [
                row for row in sources if row["id"] in state.get("read_source_ids", [])
            ],
            "unread_sources": unread_sources,
            "unfixed_ids": unfixed,
            "unread_blocks": unread,
            "checked_blocks": state["read_ids"],
            "blocks": [{"id": b["id"], "title": b["title"]} for b in state["blocks"]],
            "suggested_fixes": len(patched),
            "summary": state["summary"] or "检查结果已整理，请查看建议和修改差异。",
            "stop_reasons": list(dict.fromkeys(state.get("stop_reasons", [])))
            if state["partial"]
            else [],
        }
        if unread_sources:
            report["stop_reasons"].append(
                f"还有{len(unread_sources)}条客户原话未核对，不能视为完整检查。"
            )
        if self.job["source"].get("source_overflow"):
            report["stop_reasons"].append(
                "历史来源超过本次目录容量，较早内容尚未核对；原话仍保留。"
            )
        if report["partial"] and not state["findings"]:
            report["summary"] = "本次检查未能形成有效结论，不能据此判断草案没有问题；原稿保持不变。"
        elif not state["findings"]:
            report["summary"] = (
                "本次未提交有依据的修改建议，正文未改变；这不代表草案已经通过工程验证。"
            )
        elif not state["changes"]:
            report["summary"] = "已记录检查发现的问题，尚无可应用的修改；请查看检查依据。"
        await self.store.result(
            self.principal,
            self.project_id,
            self.item_id,
            self.review_id,
            assemble(state["blocks"], state["changes"]),
            report,
        )
        # Candidate/report are durable; the pending human decision needs no execution transcript.
        return {
            **state,
            "blocks": [],
            "messages": [],
            "pending": [],
            "findings": [],
            "changes": [],
            "loaded_sources": [],
        }

    async def wait_user(self, state):
        decision = interrupt(
            {
                "review_id": str(self.review_id),
                "message": "候选稿已保存，等待查看差异并决定是否应用。",
            }
        )
        return {"decision": decision}

    async def apply(self, state):
        result = await self.store.apply(
            self.principal,
            self.project_id,
            self.item_id,
            self.review_id,
            ApplyReview.model_validate(state["decision"]),
        )
        return {"result": result}

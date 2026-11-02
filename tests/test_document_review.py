# ruff: noqa: F811
import asyncio
import json
from uuid import UUID, uuid4

import pytest
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import act, draft, project

from backend.agents.runtime import AgentRuntime
from backend.agents.tools.review import ReviewTools, assemble
from backend.db.checkpoints import DatabaseCheckpointer
from backend.models.documents import split_markdown

SOURCE = (
    "# 系统说明\n\n## 取消预约\n已取消的预约仍占用时间。\n\n## 验收\n取消后他人能预约相同时段。"
)


def test_markdown_split_is_lossless_and_ignores_code_fences_and_nested_headings():
    text = (
        "# 草案\r\n\r\n## 数据\r\n```sql\r\n# SQL注释\r\n## 也不是标题\r\n"
        "SELECT 1;\r\n```\r\n\r\n|字段|类型|\r\n|---|---|\r\n|id|uuid|\r\n"
        "\r\n> ## 引用标题\r\n\r\n## 验收\r\n保持完整。"
    )
    blocks = split_markdown(text)
    assert "".join(b["content"] for b in blocks) == text
    assert [b["title"] for b in blocks] == ["草案", "数据", "验收"]
    assert "SELECT 1;" in blocks[1]["content"] and "|id|uuid|" in blocks[1]["content"]
    changed = split_markdown(text.replace("## 数据", "## 数据设计"), blocks)
    assert [b["id"] for b in changed] == [b["id"] for b in blocks]


def test_large_sections_split_at_real_subheadings_without_breaking_code():
    text = "# 草案\n\n## 功能\n\n### 预约\n" + "步骤说明。" * 1800 + "\n\n### 取消\n取消说明。\n"
    blocks = split_markdown(text)
    assert "".join(b["content"] for b in blocks) == text
    assert "预约" in [b["title"] for b in blocks] and "取消" in [b["title"] for b in blocks]


def tool_call(tools, state, name, arguments):
    return tools.call({"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}, state)


def test_oversized_read_gives_actionable_batch_limit_and_can_recover():
    blocks = split_markdown("\n\n".join(f"## 章节{i}\n内容{i}。" for i in range(5)))
    ids = [block["id"] for block in blocks]
    state = {"blocks": blocks, "read_ids": [], "findings": [], "changes": []}
    tools = ReviewTools({"client_quotes": []})
    result = tool_call(tools, state, "read_blocks", {"ids": ids})
    assert not result["ok"] and result["code"] == "REVIEW_BLOCK_SELECTION"
    assert "1至4" in result["message"] and not state["read_ids"]
    assert tool_call(tools, state, "read_blocks", {"ids": ids[:4]})["ok"]
    assert tool_call(tools, state, "read_blocks", {"ids": ids[4:]})["ok"]
    assert state["read_ids"] == ids


def test_patch_requires_read_evidence_and_exact_nonoverlapping_source():
    blocks = split_markdown(SOURCE)
    sid = blocks[1]["id"]
    state = {"blocks": blocks, "read_ids": [], "findings": [], "changes": [], "complete": False}
    tools = ReviewTools({"facts": {}, "client_quotes": ["取消后要释放时段。"]})
    finding = {
        "id": "f1",
        "kind": "client_mismatch",
        "severity": "major",
        "block_ids": [sid],
        "evidence": "已取消的预约仍占用时间。",
        "explanation": "与释放时段要求冲突。",
        "recommendation": "取消后释放时段。",
        "source_quote": "取消后要释放时段。",
    }
    assert not tool_call(tools, state, "record_findings", {"findings": [finding]})["ok"]
    assert tool_call(tools, state, "read_blocks", {"ids": [sid]})["ok"]
    assert tool_call(tools, state, "record_findings", {"findings": [finding]})["ok"]
    change = {
        "block_id": sid,
        "old_text": finding["evidence"],
        "new_text": "已取消的预约释放占用时段。",
        "finding_ids": ["f1"],
    }
    assert tool_call(tools, state, "propose_changes", {"changes": [change]})["ok"]
    assert not tool_call(tools, state, "propose_changes", {"changes": [change]})["ok"]
    assert assemble(blocks, state["changes"]) == SOURCE.replace(
        finding["evidence"], change["new_text"]
    )
    invalid = {**finding, "id": "f2", "source_quote": "客户从未说过"}
    assert not tool_call(tools, state, "record_findings", {"findings": [invalid]})["ok"]


class ReviewModel:
    def __init__(self, fail_verify=False):
        self.requests = 0
        self.fail_verify = fail_verify

    async def respond(self, messages, **kwargs):
        self.requests += 1
        if not kwargs.get("allow_tools"):
            return {
                "content": json.dumps(
                    {
                        "passed": not self.fail_verify,
                        "summary": "已核对修改与客户要求。",
                        "unresolved_ids": ["f1"] if self.fail_verify else [],
                    }
                )
            }, {"total_tokens": 20}
        data = json.loads(messages[1]["content"])
        if not data["read_ids"]:
            operations = [("read_blocks", {"ids": [b["id"] for b in data["manifest"]]})]
        else:
            sid = next(b["id"] for b in data["manifest"] if b["title"] == "取消预约")
            operations = [
                (
                    "record_findings",
                    {
                        "findings": [
                            {
                                "id": "f1",
                                "kind": "technical",
                                "severity": "major",
                                "block_ids": [sid],
                                "evidence": "已取消的预约仍占用时间。",
                                "explanation": "与取消释放资源的验收项冲突。",
                                "recommendation": "取消后释放占用。",
                            }
                        ]
                    },
                ),
                (
                    "propose_changes",
                    {
                        "changes": [
                            {
                                "block_id": sid,
                                "old_text": "已取消的预约仍占用时间。",
                                "new_text": "已取消的预约释放占用时段。",
                                "finding_ids": ["f1"],
                            }
                        ]
                    },
                ),
                ("finish_review", {}),
            ]
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"c-{self.requests}-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
                for i, (name, args) in enumerate(operations)
            ],
        }, {"total_tokens": 20}


def install(monkeypatch, model):
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        assert kwargs["purpose"] == "draft_reviewer"
        return await model.respond(messages, **kwargs)

    monkeypatch.setattr(AgentRuntime, "complete", complete)


async def checked(client, identity, p, item):
    path = f"/api/projects/{p['id']}/items/{item['id']}/reviews"
    body = {"request_id": str(uuid4()), "expected_version": item["version"]}
    response = await client.post(path, headers=identity, json=body)
    assert response.status_code == 202, response.text
    rid = response.json()["id"]
    for _ in range(100):
        response = await client.get(f"{path}/{rid}", headers=identity)
        assert response.status_code == 200, response.text
        if response.json()["status"] != "running":
            return response.json(), body
        await asyncio.sleep(0.02)
    pytest.fail("review did not finish")


async def test_review_hitl_apply_history_restore_and_idempotency(auth_api, monkeypatch):
    client, _, database = auth_api
    alice, bob, admin = (
        headers(await login(client)),
        headers(await login(client, "bob")),
        headers(await login(client, "admin")),
    )
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    item = await act(client, alice, item, "submit")
    item = await act(client, admin, item, "confirm")
    model = ReviewModel()
    install(monkeypatch, model)
    review, request = await checked(client, alice, p, item)
    assert review["status"] == "ready", review
    assert review["report"]["verified"] and not review["report"]["partial"]
    assert review["candidate"] != SOURCE
    base = f"/api/projects/{p['id']}/items/{item['id']}"
    assert (await client.get(base, headers=alice)).json()["content"] == SOURCE
    assert (await client.get(f"{base}/reviews/{review['id']}", headers=bob)).status_code == 404
    count = model.requests
    replay = await client.post(f"{base}/reviews", headers=alice, json=request)
    assert replay.json()["id"] == review["id"] and model.requests == count
    state = await DatabaseCheckpointer(database, UUID(p["id"])).aget_tuple(
        {"configurable": {"thread_id": f"review:{review['id']}", "checkpoint_ns": ""}}
    )
    assert state is not None
    assert not client._transport.app.state.agent_runtime.active
    action = {"request_id": str(uuid4()), "expected_version": item["version"]}
    path = f"{base}/reviews/{review['id']}/decision"
    saved = await client.post(path, headers=alice, json=action)
    assert saved.status_code == 200, saved.text
    updated = saved.json()["item"]
    assert (
        updated["status"] == "draft"
        and not updated["staff_confirmed"]
        and not updated["customer_confirmed"]
    )
    assert updated["content"] == SOURCE.replace(
        "已取消的预约仍占用时间。", "已取消的预约释放占用时段。"
    )
    assert (await client.post(path, headers=alice, json=action)).json() == saved.json()
    history = (await client.get(f"{base}/history", headers=alice)).json()
    assert len(history) == 1 and history[0]["content"] == SOURCE
    restored = await client.post(
        f"{base}/history/{history[0]['id']}/restore",
        headers=alice,
        json={"request_id": str(uuid4()), "expected_version": updated["version"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["item"]["content"] == SOURCE
    assert restored.json()["item"]["version"] == updated["version"] + 1
    assert (await client.get(f"{base}/history", headers=alice)).json()[0]["content"] == updated[
        "content"
    ]


async def test_edit_while_review_waits_keeps_candidate_and_refuses_overwrite(auth_api, monkeypatch):
    client, _, _ = auth_api
    alice = headers(await login(client))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    install(monkeypatch, ReviewModel())
    review, _ = await checked(client, alice, p, item)
    edited = await act(
        client, alice, item, "edit", title=item["title"], content=SOURCE + "\n客户新增要求。"
    )
    base = f"/api/projects/{p['id']}/items/{item['id']}"
    result = await client.post(
        f"{base}/reviews/{review['id']}/decision",
        headers=alice,
        json={"request_id": str(uuid4()), "expected_version": edited["version"]},
    )
    assert result.status_code == 409
    assert (await client.get(base, headers=alice)).json()["content"] == edited["content"]
    retained = (await client.get(f"{base}/reviews/{review['id']}", headers=alice)).json()
    assert retained["candidate"] == review["candidate"] and retained["status"] == "ready"


async def test_failed_verification_keeps_original_and_discard_needs_no_model(auth_api, monkeypatch):
    client, _, _ = auth_api
    alice = headers(await login(client))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    model = ReviewModel(fail_verify=True)
    install(monkeypatch, model)
    review, _ = await checked(client, alice, p, item)
    assert not review["report"]["verified"]
    path = f"/api/projects/{p['id']}/items/{item['id']}/reviews/{review['id']}/decision"
    rejected = await client.post(
        path, headers=alice, json={"request_id": str(uuid4()), "expected_version": item["version"]}
    )
    assert rejected.status_code == 409
    count = model.requests
    ignored = await client.post(
        path,
        headers=alice,
        json={
            "request_id": str(uuid4()),
            "expected_version": item["version"],
            "decision": "discard",
        },
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["review"]["status"] == "discarded" and model.requests == count
    assert ignored.json()["item"]["content"] == SOURCE


@pytest.mark.skipif(
    __import__("os").environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1",
    reason="Enable local PostgreSQL integration",
)
async def test_real_postgres_concurrent_review_apply_only_commits_once(postgres_auth, monkeypatch):
    from backend.agents.review.graph import run_review
    from backend.config import AgentSettings
    from backend.db.review_store import ReviewStore
    from backend.models.projects import CreateItem, ProjectError, StartConsultation
    from backend.models.review import ApplyReview, StartReview

    service = postgres_auth
    bundle = await service.login("alice", "Postgres-test-password")
    principal = await service.authenticate(bundle.access_token)
    store = ReviewStore(service.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    item = await store.create_item(
        principal,
        pid,
        CreateItem(
            request_id=uuid4(),
            kind="specification",
            title="隔离审查测试",
            content=SOURCE,
            base_revision=0,
        ),
    )
    iid = UUID(item["id"])
    install(monkeypatch, ReviewModel())
    runtime = AgentRuntime(AgentSettings())
    try:
        job, _ = await store.start(
            principal, pid, iid, StartReview(request_id=uuid4(), expected_version=item["version"])
        )
        rid = UUID(job["id"])
        await run_review(runtime, store, principal, pid, iid, rid)
        results = await asyncio.gather(
            *[
                store.apply(
                    principal,
                    pid,
                    iid,
                    rid,
                    ApplyReview(request_id=uuid4(), expected_version=item["version"]),
                )
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(r, dict) for r in results) == 1
        assert sum(isinstance(r, ProjectError) for r in results) == 1
        current = await store.item(principal, pid, iid)
        assert current["version"] == item["version"] + 1
        assert len(await store.history(principal, pid, iid)) == 1
    finally:
        await runtime.close()


def test_confirmed_customer_requirement_can_replace_old_automatic_marker():
    text = "## 权限\n【自动生成】主管可以查看全部明细。\n"
    blocks = split_markdown(text)
    sid = blocks[0]["id"]
    state = {"blocks": blocks, "read_ids": [sid], "findings": [], "changes": [], "complete": False}
    tools = ReviewTools(
        {"facts": {"users": ["主管仅看本部门汇总。"]}, "client_quotes": ["主管仅看本部门汇总。"]}
    )
    finding = {
        "id": "permission",
        "kind": "client_mismatch",
        "severity": "major",
        "block_ids": [sid],
        "evidence": "【自动生成】主管可以查看全部明细。",
        "explanation": "违背客户要求。",
        "recommendation": "按客户权限修正。",
        "source_quote": "主管仅看本部门汇总。",
    }
    assert tool_call(tools, state, "record_findings", {"findings": [finding]})["ok"]
    change = {
        "block_id": sid,
        "old_text": finding["evidence"],
        "new_text": "主管仅看本部门汇总。",
        "finding_ids": ["permission"],
    }
    assert tool_call(tools, state, "propose_changes", {"changes": [change]})["ok"]
    assert assemble(blocks, state["changes"]) == "## 权限\n主管仅看本部门汇总。\n"
    state["changes"] = []
    state["findings"][0]["kind"] = "technical"
    result = tool_call(tools, state, "propose_changes", {"changes": [change]})
    assert not result["ok"] and "自动生成" in result["message"]


def test_long_section_without_subheadings_uses_whole_paragraphs():
    text = "## 业务流程\n\n" + ("完整的操作说明。" * 80 + "\n\n") * 40
    blocks = split_markdown(text)
    assert len(blocks) > 1 and "".join(b["content"] for b in blocks) == text
    assert all(len(b["content"]) < 12000 for b in blocks)
    assert all(b["content"].endswith("\n\n") for b in blocks)


def test_invalid_finding_does_not_discard_other_valid_findings():
    blocks = split_markdown(SOURCE)
    sid = blocks[1]["id"]
    state = {"blocks": blocks, "read_ids": [sid], "findings": [], "changes": [], "complete": False}
    tools = ReviewTools({"facts": {}, "client_quotes": []})
    valid = {
        "id": "valid",
        "kind": "technical",
        "severity": "major",
        "block_ids": [sid],
        "evidence": "已取消的预约仍占用时间。",
        "explanation": "与验收要求冲突。",
        "recommendation": "取消后释放占用。",
    }
    invalid = {**valid, "id": "invalid", "evidence": "不存在的原文"}
    result = tool_call(tools, state, "record_findings", {"findings": [invalid, valid]})
    assert result["ok"] and result["accepted_ids"] == ["valid"]
    assert [f["id"] for f in state["findings"]] == ["valid"]
    assert result["rejected"][0]["id"] == "invalid"
    assert tool_call(
        tools,
        state,
        "propose_changes",
        {
            "changes": [
                {
                    "block_id": sid,
                    "finding_ids": ["valid"],
                    "old_text": valid["evidence"],
                    "new_text": "已取消的预约释放占用。",
                }
            ]
        },
    )["ok"]


async def test_short_block_context_remains_available_after_a_tool_exchange():
    from types import SimpleNamespace

    from backend.agents.review.nodes import ReviewNodes

    blocks = split_markdown(SOURCE)
    job = {"source": {"facts": {}, "client_quotes": [], "assumptions": []}}
    runtime = SimpleNamespace(settings=SimpleNamespace(review_max_rounds=8, review_max_tools=16))
    nodes = ReviewNodes(runtime, None, None, None, None, None, job)
    observed = []

    async def model(state, messages, tools=None, **kwargs):
        observed.append(json.loads(messages[1]["content"]))
        state["rounds"] += 1
        return {"tool_calls": []}

    nodes.model = model
    state = {
        "rounds": 0,
        "calls": 0,
        "blocks": blocks,
        "findings": [],
        "changes": [],
        "read_ids": [],
        "messages": [],
    }
    state = await nodes.assess(state)
    state["messages"] = [{"role": "tool", "content": '{"ok":true}'}]
    await nodes.assess(state)
    for data in observed:
        supplied = [b for b in data["manifest"] if "content" in b]
        assert "".join(b["content"] for b in supplied) == SOURCE
        assert sum(len(b["content"]) for b in supplied) <= 6000
        assert len(data["read_ids"]) == len(set(data["read_ids"]))


async def test_final_assessment_reserves_submission_and_removes_exhausted_read_tool():
    from types import SimpleNamespace

    from backend.agents.review.nodes import ReviewNodes

    runtime = SimpleNamespace(settings=SimpleNamespace(review_max_rounds=8, review_max_tools=16))
    job = {"source": {"facts": {}, "client_quotes": [], "assumptions": []}}
    nodes = ReviewNodes(runtime, None, None, None, None, None, job)
    state = {
        "rounds": 6,
        "calls": 6,
        "blocks": split_markdown(SOURCE),
        "findings": [],
        "changes": [],
        "read_ids": [],
        "messages": [],
    }

    async def model(state, messages, tools=None, **kwargs):
        assert kwargs["tool_choice"]["function"]["name"] == "finish_review"
        assert "read_blocks" not in [tool["function"]["name"] for tool in tools]
        assert "未读或未能修复" in messages[-1]["content"]
        state["rounds"] += 1
        return {
            "tool_calls": [{"id": "end", "function": {"name": "finish_review", "arguments": "{}"}}]
        }

    nodes.model = model
    result = await nodes.assess(state)
    assert result["rounds"] == 7 and result["pending"]


async def test_repeated_invalid_review_stops_and_explains_incomplete_result(auth_api, monkeypatch):
    class InvalidModel:
        def __init__(self):
            self.requests = 0

        async def respond(self, messages, **kwargs):
            self.requests += 1
            data = json.loads(messages[1]["content"])
            sid = next(b["id"] for b in data["manifest"] if b["title"] == "取消预约")
            args = {
                "findings": [
                    {
                        "id": "wrong",
                        "kind": "client_mismatch",
                        "severity": "major",
                        "block_ids": [sid],
                        "evidence": "已取消的预约仍占用时间。",
                        "source_quote": "### 客户已说明的信息\n客户没说过的内容",
                        "explanation": "错误地把草案当客户原话",
                        "recommendation": "不应采用",
                    }
                ]
            }
            return {
                "tool_calls": [
                    {
                        "id": f"bad-{self.requests}",
                        "type": "function",
                        "function": {"name": "record_findings", "arguments": json.dumps(args)},
                    }
                ]
            }, {"total_tokens": 10}

    client, _, _ = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    item = await draft(client, identity, p["id"], content=SOURCE)
    model = InvalidModel()
    install(monkeypatch, model)
    review, _ = await checked(client, identity, p, item)
    assert model.requests == 2
    assert review["report"]["partial"] and not review["report"]["changes"]
    assert "不能据此判断" in review["report"]["summary"]
    assert any("停止重复尝试" in r for r in review["report"]["stop_reasons"])


async def test_plain_review_response_gets_one_bounded_native_submission(auth_api, monkeypatch):
    class PlainThenSubmit(ReviewModel):
        async def respond(self, messages, **kwargs):
            if not kwargs.get("allow_tools"):
                return await super().respond(messages, **kwargs)
            self.requests += 1
            if not kwargs.get("tool_choice"):
                return {"content": "取消后的占用规则与验收要求存在冲突，需要修正。"}, {
                    "total_tokens": 10
                }
            assert kwargs["tool_choice"]["function"]["name"] == "finish_review"
            assert "read_blocks" not in [tool["function"]["name"] for tool in kwargs["tools"]]
            data = json.loads(messages[1]["content"])
            sid = next(b["id"] for b in data["manifest"] if b["title"] == "取消预约")
            payload = {
                "findings": [
                    {
                        "id": "f1",
                        "kind": "technical",
                        "severity": "major",
                        "block_ids": [sid],
                        "evidence": "已取消的预约仍占用时间。",
                        "explanation": "与取消验收冲突",
                        "recommendation": "取消后释放占用",
                    }
                ],
                "changes": [
                    {
                        "block_id": sid,
                        "old_text": "已取消的预约仍占用时间。",
                        "new_text": "已取消的预约释放占用时段。",
                        "finding_ids": ["f1"],
                    }
                ],
                "summary": "修正取消释放规则",
            }
            return {
                "tool_calls": [
                    {
                        "id": "submit",
                        "type": "function",
                        "function": {"name": "finish_review", "arguments": json.dumps(payload)},
                    }
                ]
            }, {"total_tokens": 10}

    client, _, _ = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    item = await draft(client, identity, p["id"], content=SOURCE)
    model = PlainThenSubmit()
    install(monkeypatch, model)
    review, _ = await checked(client, identity, p, item)
    assert model.requests == 3
    assert review["report"]["verified"] and len(review["report"]["changes"]) == 1
    assert not review["report"]["partial"]


def test_unverified_historical_stack_is_not_presented_to_models_as_customer_choice():
    from backend.agents.common import technology_has_source
    from backend.agents.drafting.state import DraftState
    from backend.agents.review.nodes import ReviewNodes

    source = {
        "facts": {"technology_preference": "Java + Spring Boot", "scope": ["会议室预约"]},
        "client_quotes": ["请把技术章节补齐"],
        "assumptions": [],
    }
    nodes = ReviewNodes(None, None, None, None, None, None, {"source": source})
    facts, unverified = nodes.source_facts()
    assert (
        "technology_preference" not in facts
        and unverified["technology_preference"] == "Java + Spring Boot"
    )
    draft = DraftState(business_notes=source["facts"], customer_statements=source["client_quotes"])
    assert "technology_preference" not in draft.context()["business_notes"]
    assert source["facts"]["technology_preference"] == "Java + Spring Boot"
    assert not technology_has_source("Java", ["使用JavaScript"])
    assert technology_has_source("Java + PostgreSQL", ["后端使用Java，数据库选PostgreSQL"])


def test_legacy_empty_review_is_not_displayed_as_clean_or_customer_confirmed():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from backend.db.review_store import ReviewStore

    row = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        item_id=uuid4(),
        status="ready",
        source={"version": 1, "content": "原稿"},
        candidate="原稿",
        report={"partial": True, "findings": [], "summary": "所有客户指定的技术均已确认"},
        events=[{"type": "tool", "ok": False}],
        error="",
        created_at=datetime.now(UTC),
    )
    output = ReviewStore.review_view(row)
    assert "不能据此判断" in output["report"]["summary"]
    assert output["report"]["stop_reasons"]
    assert row.report["summary"] == "所有客户指定的技术均已确认"

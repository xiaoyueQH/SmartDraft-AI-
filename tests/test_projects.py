# ruff: noqa: F811
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401

from backend.agents.limits import measured_tokens, request_budget
from backend.agents.orchestrator import ProjectAgent
from backend.models.discovery import merge_discovery
from backend.models.projects import ProjectError, ProjectItem, ProjectMessage, ProjectRun


async def project(client, identity, **extra):
    payload = {"request_id": str(uuid4()), **extra}
    response = await client.post("/api/consultations", headers=identity, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def draft(client, identity, pid, kind="specification", revision=0, **extra):
    response = await client.post(
        f"/api/projects/{pid}/items",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "kind": kind,
            "base_revision": revision,
            "title": "需求书初版",
            "content": "角色：库管。入库后库存增加，可按商品查询。验收：入库5件查询返回5件。",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def act(client, identity, item, action, **extra):
    response = await client.post(
        f"/api/projects/{item['project_id']}/items/{item['id']}/actions",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "expected_version": item["version"],
            "action": action,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["item"]


async def baseline(client, customer, admin, pid):
    row = await draft(client, customer, pid)
    row = await act(client, customer, row, "submit")
    row = await act(client, customer, row, "confirm")
    return await act(client, admin, row, "confirm")


async def test_project_access_and_creation_are_scoped_and_idempotent(auth_api):
    client, _, _ = auth_api
    alice, bob, admin = (
        headers(await login(client)),
        headers(await login(client, "bob")),
        headers(await login(client, "admin")),
    )
    request_id = str(uuid4())
    p = await project(client, alice, request_id=request_id)
    assert p["phase"] == "discovery" and p["brief"] == "" and p["stack"] == "待确认"
    assert not p["revision"] and not p["discovery"]
    assert await project(client, alice, request_id=request_id) == p
    for suffix in ("", "/items", "/activity", "/agent"):
        assert (
            await client.get(f"/api/projects/{p['id']}{suffix}", headers=bob)
        ).status_code == 404
    assert (await client.get("/api/projects", headers=bob)).json()["items"] == []
    assert (await client.get(f"/api/projects/{p['id']}", headers=admin)).status_code == 200
    assert (
        await client.post(
            "/api/consultations",
            headers=bob,
            json={
                "request_id": str(uuid4()),
                "owner_id": p["owner_id"],
            },
        )
    ).status_code == 403


async def test_baseline_requires_both_parties_and_edit_resets_signatures(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    row = await draft(client, customer, p["id"], questions=["仓库数量？"])
    row = await act(client, customer, row, "submit")
    response = await client.post(
        f"/api/projects/{p['id']}/items/{row['id']}/actions",
        headers=customer,
        json={"request_id": str(uuid4()), "expected_version": row["version"], "action": "confirm"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "QUESTIONS_OPEN"
    row = await act(
        client,
        customer,
        row,
        "edit",
        title=row["title"],
        content=row["content"] + "单仓库",
        questions=[],
    )
    row = await act(client, customer, row, "submit")
    row = await act(client, customer, row, "confirm")
    assert row["customer_confirmed"] and row["status"] == "submitted"
    row = await act(
        client, admin, row, "edit", title=row["title"], content=row["content"] + "仅整数库存"
    )
    assert not row["customer_confirmed"] and row["status"] == "draft"
    row = await act(client, admin, row, "submit")
    row = await act(client, admin, row, "confirm")
    row = await act(client, customer, row, "confirm")
    assert row["published_revision"] == 1 and row["status"] == "approved"
    confirmed = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()["project"]
    assert confirmed["phase"] == "project" and confirmed["title"] == row["title"]
    response = await client.post(
        f"/api/projects/{p['id']}/items/{row['id']}/actions",
        headers=admin,
        json={
            "request_id": str(uuid4()),
            "expected_version": row["version"],
            "action": "edit",
            "title": "覆盖",
            "content": "覆盖",
        },
    )
    assert response.status_code == 409


async def test_change_publishes_new_baseline_and_preserves_original(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    original = await baseline(client, customer, admin, p["id"])
    changed = await draft(
        client,
        customer,
        p["id"],
        "change",
        1,
        content=original["content"] + "\n新增低库存预警，阈值由管理员配置。",
    )
    stale = await draft(client, customer, p["id"], "change", 1)
    for identity, action in ((customer, "submit"), (admin, "confirm"), (customer, "confirm")):
        changed = await act(client, identity, changed, action)
    snapshot = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert snapshot["project"]["revision"] == 2 and snapshot["baseline"]["id"] == changed["id"]
    assert (
        await client.get(f"/api/projects/{p['id']}/items/{original['id']}", headers=customer)
    ).json()["content"] == original["content"]
    response = await client.post(
        f"/api/projects/{p['id']}/items/{stale['id']}/actions",
        headers=customer,
        json={"request_id": str(uuid4()), "expected_version": 1, "action": "submit"},
    )
    assert response.json()["error"]["code"] == "BASELINE_CHANGED"


async def test_service_ticket_complete_reopen_and_audit(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await baseline(client, customer, admin, p["id"])
    row = await draft(
        client,
        customer,
        p["id"],
        "bug",
        1,
        title="出库后库存未减少",
        content="预期减少1，实际不变；测试环境；每次可复现。",
    )
    row = await act(client, customer, row, "submit")
    denied = await client.post(
        f"/api/projects/{p['id']}/items/{row['id']}/actions",
        headers=customer,
        json={"request_id": str(uuid4()), "expected_version": row["version"], "action": "start"},
    )
    assert denied.status_code == 403
    row = await act(client, admin, row, "start")
    row = await act(client, admin, row, "deliver", note="提交修复说明及测试记录，等待客户复测")
    assert row["resolution"] and row["status"] == "awaiting_acceptance"
    row = await act(client, customer, row, "reopen", note="多件出库仍有问题")
    row = await act(client, admin, row, "deliver", note="补充多件处理结果")
    row = await act(client, customer, row, "accept", note="复测通过")
    assert row["status"] == "closed" and row["acceptance_note"] == "复测通过"
    history = (await client.get(f"/api/projects/{p['id']}/activity", headers=customer)).json()
    assert any(event["note"] == "多件出库仍有问题" for event in history)


async def test_internal_tasks_permissions_versions_and_replay(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await baseline(client, customer, admin, p["id"])
    row = await draft(client, admin, p["id"], "task", 1)
    path = f"/api/projects/{p['id']}/items/{row['id']}/actions"
    payload = {"request_id": str(uuid4()), "expected_version": row["version"], "action": "submit"}
    assert (await client.post(path, headers=customer, json=payload)).status_code == 403
    first = await client.post(path, headers=admin, json=payload)
    again = await client.post(path, headers=admin, json=payload)
    assert first.json() == again.json()
    stale = await client.post(
        path, headers=admin, json={**payload, "request_id": str(uuid4()), "action": "start"}
    )
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


async def test_agent_missing_configuration_does_not_fake_an_answer(auth_api):
    client, _, database = auth_api
    customer = headers(await login(client))
    p = await project(client, customer)
    response = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=customer,
        json={"request_id": str(uuid4()), "content": "帮我整理需求"},
    )
    assert (
        response.status_code == 503 and response.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"
    )
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRun)) == 0


async def wait_run(client, identity, pid, rid):
    for _ in range(100):
        result = (await client.get(f"/api/projects/{pid}/runs/{rid}", headers=identity)).json()
        if result["status"] != "running":
            return result
        await asyncio.sleep(0.01)
    pytest.fail("Agent did not finish")


async def test_agent_native_tools_persist_drafts_pair_and_trace_atomically(auth_api, monkeypatch):
    client, _, database = auth_api
    monkeypatch.setattr(ProjectAgent, "configured", property(lambda self: True))
    calls = []

    async def complete(self, messages, *, allow_tools, max_output_tokens=4000, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            import json

            return {
                "role": "assistant",
                "reasoning_content": "private",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "save_draft",
                            "arguments": json.dumps(
                                {
                                    "kind": "change",
                                    "title": "仓库需求书",
                                    "content": "角色库管，入库增加库存，验收时查询数量一致。",
                                    "questions": ["是否需要审核？"],
                                }
                            ),
                        },
                    }
                ],
            }, {"total_tokens": 100}
        assert messages[-1]["role"] == "tool" and "private" not in str(messages)
        return {"content": "已准备需求草稿，请确认是否需要审核。"}, {"total_tokens": 80}

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    customer = headers(await login(client))
    p = await project(client, customer)
    await baseline(client, customer, headers(await login(client, "admin")), p["id"])
    data = {"request_id": str(uuid4()), "content": "生成变更需求书"}
    response = await client.post(f"/api/projects/{p['id']}/messages", headers=customer, json=data)
    assert response.status_code == 202
    run = await wait_run(client, customer, p["id"], response.json()["id"])
    assert run["status"] == "completed" and any(e["type"] == "tool" for e in run["events"])
    assert run["events"][-1]["type"] == "saved"
    detail = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert [message["run_id"] for message in detail["messages"]] == [run["id"], run["id"]]
    repeated = await client.post(f"/api/projects/{p['id']}/messages", headers=customer, json=data)
    assert repeated.json()["id"] == run["id"] and len(calls) == 2
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectMessage)) == 2
        assert await session.scalar(select(func.count()).select_from(ProjectItem)) == 2
        item = await session.scalar(select(ProjectItem).where(ProjectItem.kind == "change"))
        assert item.status == "draft" and not item.staff_confirmed


async def test_agent_failure_preserves_raw_discussion_without_invented_facts(auth_api, monkeypatch):
    client, _, database = auth_api
    monkeypatch.setattr(ProjectAgent, "configured", property(lambda self: True))

    async def complete(self, messages, *, allow_tools, max_output_tokens=4000, **kwargs):
        raise TimeoutError("provider timeout secret details must never be exposed")

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    customer = headers(await login(client))
    p = await project(client, customer)
    response = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=customer,
        json={"request_id": str(uuid4()), "content": "整理需求"},
    )
    run = await wait_run(client, customer, p["id"], response.json()["id"])
    assert run["status"] == "failed" and "secret" not in run["answer"]
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectMessage)) == 2
        assert await session.scalar(select(func.count()).select_from(ProjectItem)) == 0


async def test_chat_reloads_each_turn_trace_even_with_repeated_text(auth_api, monkeypatch):
    import json

    client, _, _ = auth_api
    monkeypatch.setattr(ProjectAgent, "configured", property(lambda self: True))

    async def complete(self, messages, *, allow_tools, max_output_tokens=4000, **kwargs):
        from test_consultation import extracted

        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        return extracted(data["input"])

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    customer = headers(await login(client))
    p = await project(client, customer)
    run_ids = []
    for _ in range(6):
        response = await client.post(
            f"/api/projects/{p['id']}/messages",
            headers=customer,
            json={"request_id": str(uuid4()), "content": "继续"},
        )
        assert response.status_code == 202
        run = await wait_run(client, customer, p["id"], response.json()["id"])
        assert run["status"] == "completed"
        run_ids.append(run["id"])
    result = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert len(result["messages"]) == 12
    assert {run["id"] for run in result["runs"]} == set(run_ids)
    for run_id in run_ids:
        pair = [message for message in result["messages"] if message["run_id"] == run_id]
        assert [message["role"] for message in pair] == ["user", "assistant"]


def test_model_budget_reserves_input_and_caps_output_before_request():
    reserve, allowance = request_budget([{"role": "user", "content": "需求"}], False, 3000)
    assert reserve + allowance <= 3000 and allowance >= 256
    with pytest.raises(ProjectError, match="剩余用量不足"):
        request_budget([{"role": "user", "content": "需求" * 3000}], True, 4000)
    assert measured_tokens({"prompt_tokens": 80, "completion_tokens": 20}, 999) == 100
    assert measured_tokens({}, 999) == 999


async def test_discovery_remembers_business_across_turns_without_auto_creating_project(
    auth_api, monkeypatch
):
    import json

    client, _, _ = auth_api
    monkeypatch.setattr(ProjectAgent, "configured", property(lambda self: True))
    calls = 0

    async def complete(self, messages, *, allow_tools, max_output_tokens=4000, **kwargs):
        nonlocal calls
        calls += 1
        from test_consultation import extracted

        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        if calls == 2:
            assert "维修门店" in messages[1]["content"] and "漏单" in messages[1]["content"]
        notes = (
            {
                "subject": "维修预约需求沟通",
                "business_background": "线下维修门店",
                "current_workflow": "电话接单纸本记录",
                "problems": ["容易漏单"],
                "open_questions": ["谁负责排班？"],
            }
            if calls == 1
            else {
                "problems": ["重复预约"],
                "users": ["前台"],
                "constraints": ["单店"],
                "open_questions": ["希望如何预约？"],
            }
        )
        return extracted(data["input"], notes)

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    customer = headers(await login(client))
    p = await project(client, customer)
    for content in (
        "我开了一家维修门店，现在电话接单纸本记录，经常漏单",
        "前台负责排班，目前只有一家店",
    ):
        response = await client.post(
            f"/api/projects/{p['id']}/messages",
            headers=customer,
            json={"request_id": str(uuid4()), "content": content},
        )
        result = await wait_run(client, customer, p["id"], response.json()["id"])
        assert result["status"] == "completed", result
    detail = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert detail["project"]["phase"] == "discovery" and detail["project"]["revision"] == 0
    notes = detail["project"]["discovery"]
    assert notes["business_background"] == "线下维修门店" and notes["users"] == ["前台"]
    assert notes["problems"] == ["容易漏单", "重复预约"] and notes["constraints"] == ["单店"]
    assert detail["items"] == [] and len(detail["messages"]) == 4


async def test_failed_turn_does_not_commit_prepared_discovery_notes(auth_api, monkeypatch):
    import json

    client, _, _ = auth_api
    monkeypatch.setattr(ProjectAgent, "configured", property(lambda self: True))
    calls = 0

    async def complete(self, messages, *, allow_tools, max_output_tokens=4000, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            from test_consultation import extracted

            data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
            return extracted(
                data["input"],
                {
                    "business_background": "维修门店",
                    "current_workflow": "电话登记",
                    "users": ["前台"],
                    "goals": ["减少漏单"],
                    "scope": ["预约登记"],
                },
                draft_request_quote=data["input"],
            )
        raise TimeoutError("provider interrupted")

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    from backend.db.project_store import ProjectStore

    async def reject_commit(self, *args, **kwargs):
        raise ProjectError("VERSION_CONFLICT", "资料已变更", 409)

    monkeypatch.setattr(ProjectStore, "finish", reject_commit)
    customer = headers(await login(client))
    p = await project(client, customer)
    result = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=customer,
        json={"request_id": str(uuid4()), "content": "了解业务并整理草案", "intent": "chat"},
    )
    run = await wait_run(client, customer, p["id"], result.json()["id"])
    assert run["status"] == "failed"
    detail = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert detail["project"]["discovery"] == {} and len(detail["messages"]) == 2


def test_discovery_list_additions_and_explicit_corrections_preserve_other_facts():
    existing = {
        "problems": ["漏单"],
        "users": ["前台"],
        "open_questions": ["谁负责排班？", "是否多门店？"],
    }
    added = merge_discovery(existing, {"problems": ["重复预约", "漏单"], "users": ["店员"]})
    assert added["problems"] == ["漏单", "重复预约"] and added["users"] == ["前台", "店员"]
    corrected = merge_discovery(
        added, {"resolved_questions": ["谁负责排班？"], "remove_facts": {"users": ["前台"]}}
    )
    assert corrected["users"] == ["店员"] and corrected["problems"] == added["problems"]
    assert corrected["open_questions"] == ["是否多门店？"]

# ruff: noqa: F811
from uuid import UUID

import pytest
from drafting_support import DraftModel
from test_auth import auth_api, headers, login  # noqa: F401
from test_consultation import install_model, send
from test_projects import project

from backend.agents.consultation.nodes import is_draft_command
from backend.agents.orchestrator import ProjectAgent
from backend.db.project_store import ProjectStore
from backend.models.projects import ProjectError


@pytest.mark.parametrize(
    "text", ["帮我生成一份草案吧", "请整理一份需求草案", "直接出方案", "生成需求书"]
)
def test_explicit_draft_command(text):
    assert is_draft_command(text)


@pytest.mark.parametrize(
    "text",
    [
        "不要生成草案",
        "先别生成草案",
        "生成草案是什么意思",
        "如果我说生成草案呢",
        "他说帮我生成一份草案吧",
    ],
)
def test_mentions_and_negation_are_not_commands(text):
    assert not is_draft_command(text)


async def test_writer_uses_saved_notes_and_never_reextracts_or_asks_for_documents(
    auth_api, monkeypatch
):
    import json

    client, _, _ = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)
    writes = []

    async def complete(self, messages, **kwargs):
        if "DRAFT_WRITE" in messages[0]["content"]:
            data = json.loads(messages[1]["content"].split("以下为业务数据：")[1])
            writes.append(data)
            assert kwargs.get("tools") and not kwargs.get("json_output")
        return await model.respond(messages)

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(client, identity, p["id"], "先记录业务信息：手工登记和查询预约。")
    before = len(model.calls)
    result = await send(client, identity, p["id"], "帮我生成一份草案吧")
    assert result["status"] == "completed", result
    assert len(model.calls) == before + 1 and len(writes) == 1
    assert writes[0]["business_notes"]["scope"] == model.notes["scope"]
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(detail["items"]) == 1 and "客户已说明的信息" in detail["items"][0]["content"]
    assert "登记和查询预约" in detail["items"][0]["content"]
    assert detail["project"]["revision"] == 0 and detail["items"][0]["questions"]


async def test_writer_failure_returns_honest_saved_initial_draft_without_retry(
    auth_api, monkeypatch
):
    client, _, _ = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(client, identity, p["id"], "记录预约流程")
    model.fail_write = True
    before = len(model.calls)
    result = await send(client, identity, p["id"], "生成一份草案")
    assert result["status"] == "completed" and "初稿" in result["answer"]
    assert len(model.calls) == before + 1 and "private" not in str(result)
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(detail["messages"]) == 4 and len(detail["items"]) == 1
    assert "详细说明未生成" in detail["items"][0]["content"]
    assert "登记和查询预约" in detail["items"][0]["content"]


async def test_no_business_context_cannot_invent_a_draft(auth_api, monkeypatch):
    client, _, _ = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "帮我生成一份草案吧")
    assert result["status"] == "completed" and not model.calls
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert not detail["items"] and len(detail["messages"]) == 2


async def test_complete_history_checks_owner_and_version(auth_api):
    client, auth, database = auth_api
    alice = await login(client)
    bob = await login(client, "bob")
    owner = await auth.authenticate(alice["access_token"])
    other = await auth.authenticate(bob["access_token"])
    p = await project(client, headers(alice))
    store = ProjectStore(database)
    with pytest.raises(ProjectError) as error:
        await store.draft_history(other, UUID(p["id"]), p["version"], 200000)
    assert error.value.status == 404
    with pytest.raises(ProjectError) as error:
        await store.draft_history(owner, UUID(p["id"]), p["version"] + 1, 200000)
    assert error.value.code == "VERSION_CONFLICT"


@pytest.mark.parametrize("failed_section", [0, 2])
async def test_section_draft_commits_once_and_keeps_human_review(
    auth_api, monkeypatch, failed_section
):
    import json

    from test_draft_sections import plan_calls

    client, _, _ = auth_api
    model = DraftModel()
    model.notes = {**model.notes, "scope": ["预约登记", "预约查询", "冲突时段校验"]}
    install_model(monkeypatch, model)
    chapters = []

    async def complete(self, messages, **kwargs):
        system = messages[0]["content"]
        if "DRAFT_AGENT" in system:
            from test_draft_sections import SPECS, call

            if not chapters:
                return {"tool_calls": [*plan_calls(), call("write_prd_section", SPECS[0])]}, {
                    "total_tokens": 100
                }
            if len(chapters) == 1:
                return {"tool_calls": [call("write_prd_section", SPECS[1])]}, {"total_tokens": 100}
            return {"tool_calls": [call("finish_draft", {})]}, {"total_tokens": 100}
        if "DRAFT_SECTION" in system:
            data = json.loads(messages[1]["content"].split("以下为业务数据：")[1])
            chapters.append(data["current_section"]["title"])
            if len(chapters) == failed_section:
                raise ProjectError("MODEL_TRUNCATED", "private provider details", 503)
            return {"content": "【已说明】前台负责预约登记。\n" * 15}, {"total_tokens": 100}
        return await model.respond(messages)

    monkeypatch.setattr(ProjectAgent, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(client, identity, p["id"], "预约登记、查询并校验冲突时间")
    result = await send(client, identity, p["id"], "帮我生成一份草案吧")
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert result["status"] == "completed" and len(chapters) == 2
    assert len(detail["items"]) == 1 and len(detail["messages"]) == 4
    assert detail["project"]["phase"] == "discovery" and detail["project"]["revision"] == 0
    assert detail["project"]["consultation"]["status"] == "draft_ready"
    assert "private" not in str(result)
    assert ("本章暂未生成" in detail["items"][0]["content"]) == bool(failed_section)

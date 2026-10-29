# ruff: noqa: F811
import asyncio
import json
from uuid import UUID, uuid4

import pytest
from drafting_support import DraftModel
from sqlalchemy import select
from test_auth import auth_api, headers, login  # noqa: F401
from test_consultation import install_model, send
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import project

from backend.agents.runtime import AgentRuntime
from backend.db.project_store import ProjectStore
from backend.models.consultation import (
    ConsultationRecord,
    ConsultationState,
    ConsultationTurn,
    ToolSetting,
)
from backend.models.projects import (
    DraftItem,
    Project,
    ProjectError,
    ProjectMessage,
    SendMessage,
    StartConsultation,
)


def test_misplaced_reply_and_sources_are_lifted_without_relaxing_fact_validation():
    from backend.agents.consultation.state import verified_patch

    row = ConsultationTurn.model_validate(
        {
            "notes": {
                "users": ["前台"],
                "reply": "先减少重复登记。",
                "sources": [{"field": "users", "value": "前台", "source_id": "current:0"}],
            }
        }
    )
    patch, _ = verified_patch(row, "前台负责登记", "run", {})
    assert patch == {"users": ["前台"]} and row.reply == "先减少重复登记。"
    row.sources[0].source_id = "invented"
    with pytest.raises(ValueError):
        verified_patch(row, "前台负责登记", "run", {})
    with pytest.raises(ValueError):
        ConsultationTurn.model_validate({"notes": {"reply": "另一个回复"}, "reply": "冲突回复"})


def test_unsourced_unknown_placeholder_does_not_drop_other_facts_or_overwrite_known_stack():
    from backend.agents.consultation.state import verified_patch

    row = ConsultationTurn.model_validate(
        {
            "notes": {"users": ["前台"], "technology_preference": "待确认"},
            "sources": [{"field": "users", "value": "前台", "source_id": "current:0"}],
            "reply": "先讨论登记流程。",
        }
    )
    patch, _ = verified_patch(row, "前台负责登记", "run", {"technology_preference": "现有平台"})
    assert patch == {"users": ["前台"]}


def test_single_fact_protocol_builds_exact_patch_and_keeps_provenance():
    from backend.agents.consultation.state import verified_patch
    from backend.models.discovery import merge_discovery

    record = ConsultationRecord(
        facts=[
            {"field": "users", "value": "店主负责收款", "source_id": "current:0"},
            {"field": "users", "value": "店员只看订单库存", "source_id": "current:0"},
            {"field": "scope", "value": "手工订单自动扣库存", "source_id": "current:0"},
        ],
        remove_facts=[{"field": "users", "value": "店员可以退款", "source_id": "current:0"}],
    )
    turn = record.turn()
    patch, sources = verified_patch(
        turn, "更正：店员不可以退款，只看订单库存；店主收款，手工订单扣库存", "run", {}
    )
    assert len(sources) == 4
    result = merge_discovery({"users": ["店员可以退款"]}, patch)
    assert result["users"] == ["店主负责收款", "店员只看订单库存"]
    record.facts[0].source_id = "invented"
    with pytest.raises(ValueError):
        verified_patch(record.turn(), "原话", "run", {})


@pytest.mark.parametrize("remove_old", [False, True])
def test_stack_components_and_scalar_replacement_preserve_each_original_source(remove_old):
    from backend.agents.consultation.state import verified_patch
    from backend.models.discovery import merge_discovery

    prompt = "改用Java 21和Spring Boot，PostgreSQL数据库，Docker部署，不用原来的Node.js。"
    record = ConsultationRecord(
        facts=[
            {"field": "technology_preference", "value": value, "source_id": "current:0"}
            for value in ("Java 21和Spring Boot", "PostgreSQL", "Docker")
        ],
        remove_facts=[
            {"field": "technology_preference", "value": "Node.js", "source_id": "current:0"}
        ]
        if remove_old
        else [],
    )
    patch, sources = verified_patch(record.turn(), prompt, "run", {})
    result = merge_discovery({"technology_preference": "Node.js"}, patch)
    assert result["technology_preference"] == "Java 21和Spring Boot；PostgreSQL；Docker"
    assert len(sources) == 3 and all(source.quote == prompt for source in sources)
    record.facts[1].source_id = "invented"
    with pytest.raises(ValueError):
        verified_patch(record.turn(), prompt, "run", {})


def test_scalar_removal_requires_current_correction_and_clears_the_old_choice():
    from backend.agents.consultation.state import verified_patch
    from backend.models.discovery import merge_discovery

    record = ConsultationRecord(
        remove_facts=[
            {"field": "technology_preference", "value": "旧平台", "source_id": "current:0"}
        ]
    ).turn()
    with pytest.raises(ValueError):
        verified_patch(record, "继续讨论", "run", {"technology_preference": "旧平台"})
    patch, _ = verified_patch(
        record, "纠正：旧平台只是举例，不是我的选择", "run", {"technology_preference": "旧平台"}
    )
    assert (
        merge_discovery({"technology_preference": "旧平台"}, patch)["technology_preference"] == ""
    )


def test_latest_stack_components_override_valid_history_but_unknown_sources_still_fail():
    from backend.agents.consultation.state import verified_patch

    prompt = "现在改成Java和PostgreSQL"
    history = [
        {"id": "old-message", "role": "user", "content": "以前用Node.js", "run_id": "old-run"}
    ]
    record = ConsultationRecord(
        facts=[
            {"field": "technology_preference", "value": "Node.js", "source_id": "run:old-run:0"},
            {"field": "technology_preference", "value": "Java", "source_id": "current:0"},
            {"field": "technology_preference", "value": "PostgreSQL", "source_id": "current:0"},
        ]
    )
    patch, sources = verified_patch(record.turn(), prompt, "new-run", {}, history)
    assert patch["technology_preference"] == "Java；PostgreSQL" and len(sources) == 3
    record.facts[0].source_id = "unknown:0"
    with pytest.raises(ValueError):
        verified_patch(record.turn(), prompt, "new-run", {}, history)


async def test_native_fact_protocol_persists_without_reply_or_duplicated_notes(
    auth_api, monkeypatch
):
    client, _, _ = auth_api
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        if "CONSULTATION_REPLY" in messages[0]["content"]:
            return {"content": "可以先把两种订单的来源记录清楚，再核对是否已经扣过库存。"}, {
                "total_tokens": 50
            }
        return {
            "tool_calls": [
                {
                    "id": "facts",
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps(
                            {
                                "facts": [
                                    {
                                        "field": "scope",
                                        "value": "手工录单自动扣库存",
                                        "source_id": "current:0",
                                    },
                                    {
                                        "field": "goals",
                                        "value": "将录单时间压到30分钟以内",
                                        "source_id": "current:0",
                                    },
                                ],
                                "decision": {"ready": False},
                            }
                        ),
                    },
                }
            ]
        }, {"total_tokens": 100}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "手工录单自动扣库存，希望30分钟内完成")
    assert result["status"] == "completed"
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert detail["project"]["discovery"]["scope"] == ["手工录单自动扣库存"]
    assert not detail["project"]["consultation"]["memory_pending"]


async def test_resume_reads_repaired_database_notes_instead_of_old_checkpoint(
    auth_api, monkeypatch
):
    from backend.models.discovery import merge_discovery

    client, _, database = auth_api
    model = DraftModel()
    model.ready = False
    install_model(monkeypatch, model)
    seen = []

    async def complete(self, messages, **kwargs):
        if "CONSULTATION_ADVISE" in messages[0]["content"]:
            data = json.loads(messages[1]["content"].split("以下为业务数据：")[1])
            seen.append(data["previous"]["business_facts"])
        return await model.respond(messages)

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(client, identity, p["id"], "先讨论登记订单")
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.discovery = merge_discovery(row.discovery, {"goals": ["核对时间降到30分钟以内"]})
        row.version += 1
    result = await send(client, identity, p["id"], "接着讨论")
    assert result["status"] == "completed"
    assert "核对时间降到30分钟以内" in seen[-1]["goals"]


@pytest.mark.parametrize("reply", [None, {"text": "不规范的文字"}])
async def test_missing_or_malformed_reply_never_discards_valid_record(auth_api, monkeypatch, reply):
    client, _, _ = auth_api
    model = DraftModel()
    model.ready = False
    install_model(monkeypatch, model)
    counts = []

    async def complete(self, messages, **kwargs):
        counts.append(kwargs)
        if "CONSULTATION_ADVISE" in messages[0]["content"]:
            result, usage = await model.respond(messages)
            call = result["tool_calls"][0]
            payload = json.loads(call["function"]["arguments"])
            payload.pop("reply", None)
            if reply is not None:
                payload["reply"] = reply
            call["function"]["arguments"] = json.dumps(payload, ensure_ascii=False)
            return result, usage
        return {"content": "先把订单只登记一次，核对收款后直接交给发货人员，减少重复抄录。"}, {
            "total_tokens": 50
        }

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "我负责销售，她负责发货，手工订单也要扣库存。")
    assert result["status"] == "completed" and result["answer"].startswith("先把订单只登记一次")
    assert len(counts) == 2 and counts[1]["max_output_tokens"] <= 900
    assert any(e.get("tool") == "record_consultation" and e["ok"] is True for e in result["events"])
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert detail["project"]["discovery"]["users"] == model.notes["users"]
    assert not detail["project"]["consultation"]["memory_pending"]


async def test_reply_failure_still_persists_valid_record_and_shows_partial_result(
    auth_api, monkeypatch
):
    client, _, _ = auth_api
    model = DraftModel()
    model.ready = False
    install_model(monkeypatch, model)

    async def complete(self, messages, **kwargs):
        if "CONSULTATION_REPLY" in messages[0]["content"]:
            raise TimeoutError("private provider details")
        result, usage = await model.respond(messages)
        call = result["tool_calls"][0]
        payload = json.loads(call["function"]["arguments"])
        payload.pop("reply")
        call["function"]["arguments"] = json.dumps(payload)
        return result, usage

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "我负责登记订单，她负责发货")
    assert result["status"] == "completed" and "回复暂未完成" in result["answer"]
    assert "private" not in str(result)
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert detail["project"]["discovery"]["scope"] == model.notes["scope"]


async def test_plain_conversation_preserves_natural_reply_with_one_call(auth_api, monkeypatch):
    client, _, _ = auth_api
    calls = []

    async def complete(self, messages, **kwargs):
        calls.append(messages)
        return {
            "content": (
                "先把接单和发货串起来就能减少重复录入。订阅配送是否放在第一期，我们可以继续讨论。"
            )
        }, {"total_tokens": 50}

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "两个人做生意，先做哪些比较好？")
    assert result["status"] == "completed" and len(calls) == 1
    assert (
        result["answer"]
        == "先把接单和发货串起来就能减少重复录入。订阅配送是否放在第一期，我们可以继续讨论。"
    )
    assert "为了整理需求" not in result["answer"]
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert (
        len(detail["messages"]) == 2
        and detail["project"]["discovery"] == {}
        and not detail["items"]
    )


async def test_paraphrased_control_quotes_do_not_fail_or_force_drafting(auth_api, monkeypatch):
    client, _, _ = auth_api
    model = DraftModel()
    model.ready = False
    model.request_quote = "用户要求立即生成草案"
    model.pause_quote = "用户暂不生成草案"
    install_model(monkeypatch, model)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "我负责销售，她负责发货")
    assert result["status"] == "completed" and "权限按实际分工" in result["answer"]
    assert not any(event.get("tool") == "generate_prd" for event in result["events"])
    assert any(event.get("code") == "CONTROL_QUOTE_INVALID" for event in result["events"])
    assert len(model.calls) == 1


@pytest.mark.parametrize("control_field", ["decision", "draft_request_quote"])
async def test_verbatim_template_question_is_not_permission_to_generate(
    auth_api, monkeypatch, control_field
):
    client, _, _ = auth_api
    prompt = (
        "愿意尝试。我们内部确实重度依赖企业微信，如果能用简道云这种低代码工具在企微里搭一个"
        "轻量级的工单流转，前期投入不高的话，完全可以先跑个试点。你们有现成的企微+简道云"
        "（或类似工具）的售后工单模板吗？我们可以先内部测试一下。"
    )
    reply = (
        "可以先参考售后工单的示例字段和流转步骤做小范围试点。"
        "我没有接入您提到的平台模板库，不能保证有可直接导入的模板。"
    )
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        assert "DRAFT_" not in messages[0]["content"], (
            "Template discussions must not call the draft agent"
        )
        if "CONSULTATION_REPLY" in messages[0]["content"]:
            return {"content": reply}, {"total_tokens": 50}
        record = {
            "notes": {"scope": ["售后工单流转"]},
            "sources": [{"field": "scope", "value": "售后工单流转", "source_id": "current:0"}],
            "reply": reply,
        }
        if control_field == "decision":
            record["decision"] = {"request_quote": prompt}
        else:
            record["draft_request_quote"] = prompt
        return {
            "content": reply,
            "tool_calls": [
                {
                    "id": "record",
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps(record, ensure_ascii=False),
                    },
                }
            ],
        }, {"total_tokens": 100}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], prompt)
    assert result["status"] == "completed" and reply in result["answer"]
    assert "生成并保存" not in result["answer"]
    assert not any(event.get("tool") == "generate_prd" for event in result["events"])
    assert any(event.get("code") == "CONTROL_QUOTE_INVALID" for event in result["events"])
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert detail["items"] == []
    assert detail["project"]["discovery"]["scope"] == ["售后工单流转"]


@pytest.mark.skipif(
    __import__("os").environ.get("SUPPORTFLOW_TEST_LIVE_MODEL") != "1",
    reason="Explicit opt-in for a paid model replay in isolated PostgreSQL",
)
async def test_live_template_question_keeps_consultation_without_generating(postgres_auth):
    from backend.config import get_settings

    service = postgres_auth
    principal = await service.authenticate(
        (await service.login("alice", "Postgres-test-password")).access_token
    )
    store = ProjectStore(service.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    prompt = (
        "愿意尝试。我们内部确实重度依赖企业微信，如果能用简道云这种低代码工具在企微里搭一个"
        "轻量级的工单流转，前期投入不高的话，完全可以先跑个试点。你们有现成的企微+简道云"
        "（或类似工具）的售后工单模板吗？我们可以先内部测试一下。"
    )
    row, _ = await store.start_run(principal, pid, SendMessage(request_id=uuid4(), content=prompt))
    runtime = AgentRuntime(get_settings().agent)
    try:
        runtime.reserve()
        await runtime.execute(store, principal, pid, UUID(row["id"]), prompt)
        result = await store.run(principal, pid, UUID(row["id"]))
        assert result["status"] == "completed", result["answer"]
        assert not any(event.get("tool") == "generate_prd" for event in result["events"])
        assert any(term in result["answer"] for term in ("模板", "示例", "工单"))
        detail = await store.detail(principal, pid, summaries=True)
        assert not detail["items"] and detail["project"]["discovery"]
        assert "已根据保存的业务信息生成并保存" not in result["answer"]
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "global_enabled,user_enabled", [(True, True), (False, True), (True, False)]
)
async def test_advisor_search_is_optional_and_never_sends_client_text(
    auth_api, monkeypatch, global_enabled, user_enabled
):
    from backend.agents.consultation import nodes

    client, _, database = auth_api
    calls, requests = [], []

    async def complete(self, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1 and len(kwargs.get("tools") or []) == 2:
            return {
                "tool_calls": [
                    {
                        "id": "search",
                        "function": {"name": "web_search", "arguments": '{"topic":"permissions"}'},
                    }
                ]
            }, {"total_tokens": 30}
        return {"content": "可以先按实际分工限制查看范围，具体权限还需要你们决定。"}, {
            "total_tokens": 30
        }

    async def search(client, query, **kwargs):
        requests.append(query)
        return []

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    monkeypatch.setattr(nodes, "search_public", search)
    async with database.session() as session:
        session.add(ToolSetting(name="web_search", enabled=global_enabled, version=1))
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(
        client, identity, p["id"], "私人店铺的内部权限如何分工？", web_search=user_enabled
    )
    assert result["status"] == "completed"
    assert len(requests) == (1 if global_enabled and user_enabled else 0)
    assert all(query == nodes.PUBLIC_TOPICS["permissions"] for query in requests)


@pytest.mark.skipif(
    __import__("os").environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="requires PostgreSQL"
)
async def test_real_pg_discussion_receipt_is_idempotent_and_finalization_is_versioned(
    postgres_auth,
):
    auth = postgres_auth
    principal = await auth.authenticate(
        (await auth.login("alice", "Postgres-test-password")).access_token
    )
    store = ProjectStore(auth.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    request = SendMessage(request_id=uuid4(), content="先讨论订单")
    row, _ = await store.start_run(principal, pid, request)
    rid = UUID(row["id"])
    state = ConsultationState(discussion_run_id=str(rid))

    async def stage():
        return await store.finish(
            principal,
            pid,
            rid,
            0,
            "先讨论，再起草。",
            [],
            consultation=state.model_dump(),
            expected_version=p["version"],
            discussion_only=True,
            defer_completion=True,
        )

    versions = await asyncio.gather(stage(), stage())
    assert versions[0] == versions[1] == p["version"] + 1
    state.last_run_id = str(rid)
    data = DraftItem(
        kind="specification",
        title="需求草案",
        content="这是足够完整的需求正文，用于核对并发提交没有重复。",
    )

    async def finish():
        return await store.finish(
            principal,
            pid,
            rid,
            0,
            "草案已准备，供您审阅。",
            [data.model_dump()],
            consultation=state.model_dump(),
            expected_version=versions[0],
            continue_discussion=True,
        )

    await asyncio.gather(finish(), finish())
    detail = await store.detail(principal, pid)
    assert len(detail["messages"]) == 2 and len(detail["items"]) == 1
    assert detail["messages"][1]["content"] == "草案已准备，供您审阅。"
    previous, created = await store.start_run(principal, pid, request)
    assert not created and previous["id"] == str(rid)
    async with store.database.session() as session:
        project_row = await session.get(Project, pid)
        project_row.version += 1
    with pytest.raises(ProjectError) as error:
        await stage()
    assert error.value.code == "VERSION_CONFLICT"


async def test_interruption_after_discussion_commit_preserves_pair(auth_api, monkeypatch):
    client, auth, database = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)

    async def complete(self, messages, **kwargs):
        if "DRAFT_WRITE" in messages[0]["content"]:
            raise asyncio.CancelledError()
        return await model.respond(messages)

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(client, identity, p["id"], "我负责销售，她负责发货")
    result = await send(client, identity, p["id"], "帮我生成一份草案吧")
    # The preliminary discussion was saved, but document generation was cancelled.
    assert result["status"] == "failed" and "整理中断" in result["answer"]
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(ProjectMessage).where(ProjectMessage.run_id == UUID(result["id"]))
            )
        )
    assert (
        len(rows) == 2
        and next(row.content for row in rows if row.role == "assistant") == result["answer"]
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_first_model_failure_preserves_raw_message_and_safe_failure_reply(
    auth_api, monkeypatch, cancel
):
    client, _, database = auth_api
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise TimeoutError("private vendor details")

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    identity = headers(await login(client))
    p = await project(client, identity)
    result = await send(client, identity, p["id"], "我负责销售，她负责发货")
    assert result["status"] == "failed" and "private" not in result["answer"]
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert (
        len(detail["messages"]) == 2
        and not detail["items"]
        and detail["project"]["discovery"] == {}
    )
    assert detail["messages"][0]["content"] == "我负责销售，她负责发货"
    assert detail["messages"][1]["content"] == result["answer"]
    await ProjectStore(database).fail(UUID(result["id"]), "重复处理")
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(detail["messages"]) == 2


def test_filling_a_chapter_does_not_confirm_an_automatic_technology_stack():
    from backend.agents.consultation.state import verified_patch
    from backend.models.consultation import Extraction

    prompt = "请把技术选型章节补齐，不用再向我确认。"
    extraction = Extraction.model_validate(
        {
            "reply": "已收到",
            "notes": {
                "technology_preference": "Java 21 + Spring Boot 3.3",
                "scope": ["会议室预约"],
            },
            "sources": [
                {
                    "field": "technology_preference",
                    "value": "Java 21 + Spring Boot 3.3",
                    "quote": prompt,
                },
                {"field": "scope", "value": "会议室预约", "quote": "会议室预约"},
            ],
        }
    )
    patch, sources = verified_patch(
        extraction,
        prompt,
        "run",
        {},
        [{"id": "old", "run_id": "old-run", "role": "user", "content": "我需要会议室预约"}],
    )
    assert "technology_preference" not in patch
    assert patch["scope"] == ["会议室预约"]
    assert all(s.field != "technology_preference" for s in sources)


@pytest.mark.parametrize(
    "prompt",
    [
        "我是后勤，不懂Java、Python😂，先只管办公电脑报修。",
        "Java和Python只是我举例，不是我选的技术，先只管办公电脑报修。",
    ],
)
def test_neutral_technology_mentions_do_not_turn_into_constraints(prompt):
    from backend.agents.consultation.state import verified_patch

    record = ConsultationRecord(
        facts=[
            {"field": "technology_preference", "value": "Java + Python", "source_id": "current:0"},
            {"field": "constraints", "value": "不能使用Java、Python", "source_id": "current:0"},
            {"field": "constraints", "value": "要求零代码方案", "source_id": "current:0"},
            {"field": "scope", "value": "办公电脑报修", "source_id": "current:0"},
        ]
    )
    patch, sources = verified_patch(record.turn(), prompt, "run", {})
    assert patch == {"scope": ["办公电脑报修"]}
    assert len(sources) == 1


def test_explicit_technical_choices_remain_valid_and_specific_quotes_are_checked():
    from backend.agents.consultation.state import verified_patch

    prompt = "不懂Java，但确定使用Python。必须PostgreSQL，不要Node.js。"
    record = ConsultationRecord(
        facts=[
            {
                "field": "technology_preference",
                "value": "Python + PostgreSQL",
                "source_id": "current:0",
                "intent": "required",
            },
            {
                "field": "constraints",
                "value": "不要Node.js",
                "source_id": "current:0",
                "intent": "excluded",
            },
        ]
    )
    patch, _ = verified_patch(record.turn(), prompt, "run", {})
    assert patch["technology_preference"] == "Python + PostgreSQL"
    assert patch["constraints"] == ["不要Node.js"]
    record.facts[0].quote = "客户选择了Python"
    _, sources = verified_patch(record.turn(), prompt, "run", {})
    assert sources[0].quote == prompt
    record.facts[0].source_id = "invented"
    with pytest.raises(ValueError):
        verified_patch(record.turn(), prompt, "run", {})


@pytest.mark.parametrize("prompt", ["不要使用Java", "不是Java，是Python", "不能选Java，使用Python"])
def test_excluded_technology_cannot_be_saved_as_a_positive_choice(prompt):
    from backend.agents.consultation.state import verified_patch

    record = ConsultationRecord(
        facts=[
            {"field": "technology_preference", "value": "Java", "source_id": "current:0"},
            {
                "field": "constraints",
                "value": prompt.split("，")[0],
                "source_id": "current:0",
                "intent": "excluded",
            },
        ]
    )
    patch, sources = verified_patch(record.turn(), prompt, "run", {})
    assert patch == {"constraints": [prompt.split("，")[0]]}
    assert len(sources) == 1


def test_empty_optional_fact_does_not_discard_real_business_information():
    from backend.agents.consultation.state import verified_patch

    record = ConsultationRecord.model_validate(
        {
            "facts": [
                {"field": "technology_preference", "value": "", "source_id": "current:0"},
                {"field": "scope", "value": "办公电脑报修", "source_id": "current:0"},
            ]
        }
    )
    patch, _ = verified_patch(record.turn(), "只做办公电脑报修", "run", {})
    assert patch == {"scope": ["办公电脑报修"]}


def test_removing_unknown_technology_placeholder_does_not_poison_other_facts():
    from backend.agents.consultation.state import verified_patch

    record = ConsultationRecord(
        facts=[{"field": "scope", "value": "办公电脑报修", "source_id": "current:0"}],
        remove_facts=[
            {"field": "technology_preference", "value": "待确认", "source_id": "old-message:0"}
        ],
    )
    patch, sources = verified_patch(
        record.turn(), "首期只做办公电脑报修", "run", {"technology_preference": "待确认"}
    )
    assert patch == {"scope": ["办公电脑报修"]} and len(sources) == 1


def test_removal_of_a_nonexistent_technical_fact_is_a_noop_not_a_batch_failure():
    from backend.agents.consultation.state import verified_patch

    record = ConsultationRecord(
        facts=[{"field": "scope", "value": "办公电脑报修", "source_id": "current:0"}],
        remove_facts=[
            {
                "field": "technology_preference",
                "value": "Java, Python",
                "source_id": "old:0",
                "quote": "模型拼接的无效引文",
            }
        ],
    )
    patch, sources = verified_patch(
        record.turn(), "只做办公电脑报修", "run", {"technology_preference": "待确认"}
    )
    assert patch == {"scope": ["办公电脑报修"]} and len(sources) == 1


def test_catalog_id_is_authoritative_and_unverified_quote_is_never_stored():
    from backend.agents.consultation.state import verified_patch

    prompt = "纠正：技术没有选择，只做办公电脑报修"
    history = [{"id": "old", "role": "user", "content": "以前只用群聊", "run_id": "old-run"}]
    record = ConsultationRecord(
        facts=[
            {
                "field": "scope",
                "value": "办公电脑报修",
                "source_id": "current:0",
                "quote": "只做办公电脑报修",
            }
        ]
    )
    patch, sources = verified_patch(record.turn(), prompt, "run", {}, history)
    assert patch == {"scope": ["办公电脑报修"]}
    assert sources[0].run_id == "run" and sources[0].message_id == ""
    record.facts[0].quote = "伪造的用户原话"
    _, sources = verified_patch(record.turn(), prompt, "run", {}, history)
    assert sources[0].quote == prompt
    record.facts[0].source_id = "assistant:0"
    with pytest.raises(ValueError):
        verified_patch(record.turn(), prompt, "run", {}, history)


def test_question_budget_handles_long_list_and_repeated_questions_without_losing_statements():
    from backend.agents.consultation.state import limit_questions

    reply = (
        "先处理漏单。\n1. 谁接单？\n2. 怎么分派？\n"
        "3. 用什么语言？\n4. 什么界面？\n这些可以逐步讨论。"
    )
    public, questions = limit_questions(reply)
    assert len(questions) == public.count("？") == 2
    assert "谁接单" in public and "怎么分派" in public
    assert "什么语言" not in public and "这些可以逐步讨论" in public
    public, questions = limit_questions(reply, asked=["谁接单？", "怎么分派？"], limit=0)
    assert not questions and "？" not in public and "先处理漏单" in public


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_record_protocol_repair_is_bounded_and_preserves_valid_facts(
    auth_api, monkeypatch, repair_succeeds
):
    client, _, _ = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    calls = []

    async def complete(self, messages, **kwargs):
        calls.append(kwargs.get("tool_choice"))
        facts = (
            [{"field": "scope", "value": "办公电脑报修", "source_id": "current:0"}]
            if repair_succeeds and len(calls) == 2
            else [{"field": "invalid-field", "value": "错误字段", "source_id": "current:0"}]
        )
        return {
            "content": "已收到您的说明。",
            "tool_calls": [
                {
                    "id": "record",
                    "type": "function",
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps({"facts": facts}, ensure_ascii=False),
                    },
                }
            ],
        }, {"total_tokens": 100}

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    run = await send(client, identity, p["id"], "首期只做办公电脑报修，先记录不生成")
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(calls) == 2 and all(
        choice["function"]["name"] == "record_consultation" for choice in calls
    )
    if repair_succeeds:
        assert detail["project"]["discovery"]["scope"] == ["办公电脑报修"]
        assert not detail["project"]["consultation"]["memory_pending"]
    else:
        assert detail["project"]["discovery"] == {} and "原话已保留" in run["answer"]


async def test_conversation_saves_correction_and_bounds_questions_in_persisted_reply(
    auth_api, monkeypatch
):
    from backend.models.discovery import merge_discovery

    client, _, database = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.discovery = merge_discovery(
            {},
            {
                "technology_preference": "Java",
                "constraints": ["禁止使用Java和Python"],
            },
        )

    async def complete(self, messages, **kwargs):
        payload = ConsultationRecord(
            facts=[{"field": "scope", "value": "办公电脑报修", "source_id": "current:0"}],
            remove_facts=[
                {"field": "technology_preference", "value": "Java", "source_id": "current:0"},
                {"field": "constraints", "value": "禁止使用Java和Python", "source_id": "current:0"},
            ],
        ).model_dump()
        return {
            "content": "已记录。谁接单？怎么分派？喜欢什么界面？",
            "tool_calls": [
                {
                    "id": "record",
                    "type": "function",
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps(payload, ensure_ascii=False),
                    },
                }
            ],
        }, {"total_tokens": 100}

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    result = await send(
        client, identity, p["id"], "纠正：Java和Python只是举例，不是我的选择；先只管办公电脑报修。"
    )
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert result["answer"].count("？") == 2
    assert detail["project"]["discovery"]["technology_preference"] == ""
    assert detail["project"]["discovery"]["constraints"] == []
    assert detail["project"]["discovery"]["scope"] == ["办公电脑报修"]
    assert len(detail["project"]["consultation"]["asked_questions"]) == 2

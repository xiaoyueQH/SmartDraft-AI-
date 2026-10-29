# ruff: noqa: F811
import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_auth import PASSWORD, auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import project, wait_run

from backend.agents.consultation.graph import run_fact_graph
from backend.agents.runtime import AgentRuntime
from backend.config import AgentSettings
from backend.db.conversation_store import ConversationStore
from backend.db.project_store import append_message, now
from backend.db.review_store import ReviewStore
from backend.models.consultation import FactEvidence
from backend.models.projects import (
    ConversationAction,
    Project,
    ProjectMessage,
    SendMessage,
    StartConsultation,
    human_help_requested,
)


async def state(client, identity, pid):
    result = await client.get(f"/api/projects/{pid}/conversation", headers=identity)
    assert result.status_code == 200, result.text
    return result.json()


async def control(client, identity, pid, action, **extra):
    status = (await state(client, identity, pid))["conversation"]
    response = await client.post(
        f"/api/projects/{pid}/conversation/actions",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "expected_version": status["version"],
            "action": action,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


async def send_chat(client, identity, pid, text, request_id=None):
    response = await client.post(
        f"/api/projects/{pid}/messages",
        headers=identity,
        json={"request_id": request_id or str(uuid4()), "content": text},
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize(
    "text",
    [
        "转人工",
        "请转人工",
        "我想找客服",
        "我要人工客服",
        "联系人工",
        "这个问题没解决，转人工吧",
        "人工",
        "我需要一位人工客服帮助",
        "我想找人工客服来帮我处理这个问题",
        "请问可以转人工吗",
        "我要转人工",
    ],
)
def test_explicit_human_request(text):
    assert human_help_requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "不要转人工",
        "如果客户说转人工怎么办",
        "怎么联系人工",
        "我负责客服工作",
        "我们需要客服管理功能",
    ],
)
def test_mentions_are_not_human_requests(text):
    assert not human_help_requested(text)


async def test_customer_can_call_human_and_chat_without_any_model(auth_api, monkeypatch):
    client, _, _ = auth_api
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: False))
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    requested = await send_chat(client, customer, p["id"], "请转人工")
    assert requested["delivery"] == "chat" and requested["conversation"]["mode"] == "human"
    inbox = await client.get("/api/conversations/requests", headers=admin)
    assert p["id"] in [item["id"] for item in inbox.json()["items"]]
    claimed = await control(client, admin, p["id"], "takeover")
    assert claimed["mode"] == "human" and claimed["support_id"]
    await send_chat(client, admin, p["id"], "您好，我是人工客服，请告诉我需要协助的地方。")
    await send_chat(client, customer, p["id"], "员工只能查看自己部门的订单。")
    snapshot = await state(client, customer, p["id"])
    kinds = [row["sender_type"] for row in snapshot["messages"]]
    assert "support" in kinds and "customer" in kinds and "ai" not in kinds
    assert len({row["sequence"] for row in snapshot["messages"]}) == len(snapshot["messages"])
    assert snapshot["conversation"]["extraction_pending"]


async def test_human_message_retry_keeps_receipt_after_return_to_ai(auth_api):
    client, _, database = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, customer, p["id"], "request_human")
    await control(client, admin, p["id"], "takeover")
    rid = str(uuid4())
    first = await send_chat(client, customer, p["id"], "预约信息要保留。", rid)
    await control(client, admin, p["id"], "release")
    second = await send_chat(client, customer, p["id"], "预约信息要保留。", rid)
    assert first == second and first["delivery"] == "chat"
    async with database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ProjectMessage)
                .where(ProjectMessage.content == "预约信息要保留。")
            )
            == 1
        )
    reused = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=customer,
        json={"request_id": rid, "content": "不同内容"},
    )
    assert reused.status_code == 409


async def test_admin_cannot_impersonate_customer_or_steal_active_assignment(auth_api):
    client, service, _ = auth_api
    await service.create_user("other_admin", "客服二", "admin", PASSWORD)
    customer, admin, other = (
        headers(await login(client)),
        headers(await login(client, "admin")),
        headers(await login(client, "other_admin")),
    )
    p = await project(client, customer)
    blocked = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=admin,
        json={"request_id": str(uuid4()), "content": "管理员不应冒充客户"},
    )
    assert blocked.status_code == 409
    await control(client, admin, p["id"], "takeover")
    current = (await state(client, other, p["id"]))["conversation"]
    blocked = await client.post(
        f"/api/projects/{p['id']}/conversation/actions",
        headers=other,
        json={
            "request_id": str(uuid4()),
            "expected_version": current["version"],
            "action": "takeover",
        },
    )
    assert blocked.status_code == 409
    other_user = (await client.get("/api/auth/me", headers=other)).json()
    await control(client, admin, p["id"], "transfer", support_id=other_user["id"])
    await send_chat(client, other, p["id"], "已由第二位客服接待。")
    blocked = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=admin,
        json={"request_id": str(uuid4()), "content": "旧客服不能继续发言"},
    )
    assert blocked.status_code == 409


async def test_handover_waits_for_current_ai_and_preserves_result(auth_api, monkeypatch):
    client, _, _ = auth_api
    entered, finish = asyncio.Event(), asyncio.Event()
    cancelled = False
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        nonlocal cancelled
        system = messages[0]["content"]
        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        if "CONSULTATION_FACTS" in system:
            sid = data["source_catalog"][0]["id"]
            record = {"facts": [{"field": "scope", "value": "支持订单查询", "source_id": sid}]}
        else:
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            record = {
                "facts": [{"field": "users", "value": "前台", "source_id": "current:0"}],
                "reply": "已理解您目前的接单流程，我们可以继续讨论。",
            }
        return {
            "content": record.get("reply", ""),
            "tool_calls": [
                {
                    "id": "record",
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps(record, ensure_ascii=False),
                    },
                }
            ],
        }, {"total_tokens": 80}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    running = await send_chat(client, customer, p["id"], "我们前台每天负责接单。")
    await asyncio.wait_for(entered.wait(), 3)
    requested = await control(client, customer, p["id"], "request_human")
    assert requested["mode"] == "pending" and not cancelled
    pending = await control(client, admin, p["id"], "takeover")
    assert pending["mode"] == "pending"
    new_message = await send_chat(client, customer, p["id"], "还要支持订单查询。")
    assert new_message["delivery"] == "chat" and not cancelled
    finish.set()
    result = await wait_run(client, customer, p["id"], running["id"])
    assert result["status"] == "completed" and "接单流程" in result["answer"]
    for _ in range(80):
        snapshot = await state(client, customer, p["id"])
        if (
            snapshot["conversation"]["mode"] == "human"
            and not snapshot["conversation"]["extraction_pending"]
        ):
            break
        await asyncio.sleep(0.02)
    assert snapshot["conversation"]["mode"] == "human" and not cancelled
    assert any(
        row["sender_type"] == "ai" and "接单流程" in row["content"] for row in snapshot["messages"]
    )
    assert "支持订单查询" in snapshot["discovery"]["scope"]
    await send_chat(client, admin, p["id"], "您好，接下来由我继续协助。")


async def test_history_is_paginated_searchable_and_scoped(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    customer, bob, admin = (
        headers(tokens),
        headers(await login(client, "bob")),
        headers(await login(client, "admin")),
    )
    p, second = await project(client, customer), await project(client, customer)
    principal = await service.authenticate(tokens["access_token"])
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        for index in range(55):
            append_message(
                session, row, f"第{index}条客户记录", "customer", sender=principal.account
            )
        other = await session.get(Project, UUID(second["id"]))
        append_message(session, other, "另一个项目的记录", "customer", sender=principal.account)
    page = (
        await client.get(f"/api/projects/{p['id']}/messages/history?limit=20", headers=customer)
    ).json()
    assert len(page["items"]) == 20 and page["has_more"]
    next_page = (
        await client.get(
            f"/api/projects/{p['id']}/messages/history?limit=20&before={page['next_before']}",
            headers=customer,
        )
    ).json()
    assert not set(row["id"] for row in page["items"]) & set(
        row["id"] for row in next_page["items"]
    )
    params = {"customer_id": tokens["user"]["id"], "project_id": p["id"], "limit": 100}
    all_rows = await client.get("/api/conversations/history", headers=admin, params=params)
    assert len(all_rows.json()["items"]) == 55
    params.pop("project_id")
    assert (
        len(
            (await client.get("/api/conversations/history", headers=admin, params=params)).json()[
                "items"
            ]
        )
        == 56
    )
    target = page["items"][8]
    path = f"/api/projects/{p['id']}/messages/{target['id']}/context"
    context = (await client.get(path, headers=customer)).json()
    assert target["id"] in [row["id"] for row in context["items"]] and len(context["items"]) <= 21
    assert (await client.get(path, headers=bob)).status_code == 404
    assert (
        await client.get("/api/conversations/history", headers=bob, params=params)
    ).status_code == 404
    assert (await client.get("/api/conversations/requests", headers=bob)).status_code == 403


async def test_background_fact_failure_does_not_fail_human_delivery(auth_api, monkeypatch):
    client, _, database = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, admin, p["id"], "takeover")
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    calls = 0

    async def failed(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError()

    monkeypatch.setattr(AgentRuntime, "complete", failed)
    delivered = await send_chat(client, customer, p["id"], "店员不能查看财务。")
    assert delivered["delivery"] == "chat"
    for _ in range(30):
        snapshot = await state(client, customer, p["id"])
        if snapshot["conversation"]["extraction_error"]:
            break
        await asyncio.sleep(0.01)
    assert (
        snapshot["conversation"]["extraction_pending"]
        and snapshot["conversation"]["extraction_error"]
    )
    assert not any(row["sender_type"] == "ai" for row in snapshot["messages"])
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.extraction_started_at = now() - timedelta(seconds=40)
    await state(client, customer, p["id"])
    await asyncio.sleep(0.03)
    assert (
        calls == 1
    )  # Passive reads must not loop paid retries after an invalid/failed extraction.
    await send_chat(client, admin, p["id"], "已收到，后续继续核对这条权限要求。")


async def test_silent_corrections_use_customer_original_and_exclude_support_proposals(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    identity, admin = headers(tokens), headers(await login(client, "admin"))
    principal = await service.authenticate(tokens["access_token"])
    p = await project(client, identity)
    pid = UUID(p["id"])
    await control(client, admin, p["id"], "takeover")
    async with database.session() as session:
        row = await session.get(Project, pid)
        row.discovery = {**row.discovery, "scope": ["会员积分"]}
    await send_chat(client, admin, p["id"], "建议增加优惠券。")
    original = "纠正一下，不做会员积分，只需要预约。项目叫预约管理，经营宠物店，技术用Python。"
    delivered = await send_chat(client, identity, p["id"], original)
    store, agent = ConversationStore(database), AgentRuntime(AgentSettings())
    job = await store.claim_facts(principal, pid)
    assert job and [m["content"] for m in job["messages"]] == [original]

    attempts = 0

    async def complete(messages, **kwargs):
        nonlocal attempts
        attempts += 1
        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        sid = data["source_catalog"][0]["id"]
        record = {
            "facts": [
                {"field": "scope", "value": "预约", "source_id": sid},
                {"field": "subject", "value": "预约管理", "source_id": sid},
                {"field": "business_background", "value": "经营宠物店", "source_id": sid},
                {"field": "technology_preference", "value": "Python", "source_id": sid},
            ],
            "remove_facts": [{"field": "scope", "value": "会员积分", "source_id": sid}],
        }
        if attempts == 1:
            record["resolved_questions"] = []  # Reproduce provider's invalid top-level field.
        else:
            assert data["record_retry"] and data["record_errors"][0]["field"] == [
                "resolved_questions"
            ]
        return {
            "tool_calls": [
                {
                    "function": {
                        "name": "record_consultation",
                        "arguments": json.dumps(record, ensure_ascii=False),
                    }
                }
            ]
        }, {}

    agent.complete = complete
    try:
        await run_fact_graph(agent, store, principal, pid, job)
    finally:
        await agent.close()
    snapshot = await state(client, identity, p["id"])
    assert snapshot["discovery"]["scope"] == ["预约"]
    assert attempts == 2
    assert not snapshot["conversation"]["extraction_pending"]
    assert not any(m["sender_type"] == "ai" for m in snapshot["messages"])
    async with database.session() as session:
        evidence = list(
            await session.scalars(select(FactEvidence).where(FactEvidence.project_id == pid))
        )
        assert len(evidence) == 5
        assert all(row.source["message_id"] == delivered["message"]["id"] for row in evidence)
        originals = list((await session.execute(select(ReviewStore.source_query(pid)))).mappings())
        assert len(originals) == 1 and "纠正一下" in originals[0]["content"]
        row = await session.get(Project, pid)
        assert (row.title, row.brief, row.stack) == ("预约管理", "经营宠物店", "Python")


@pytest.mark.skipif(os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="local PG opt-in")
async def test_real_postgres_parallel_human_messages_keep_sequence_and_identity(postgres_auth):
    service = postgres_auth
    token = await service.login("alice", "Postgres-test-password")
    principal = await service.authenticate(token.access_token)
    store = ConversationStore(service.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    await store.action(
        principal,
        pid,
        ConversationAction(request_id=uuid4(), expected_version=1, action="request_human"),
    )
    await asyncio.gather(
        *(
            store.send_chat(
                principal, pid, SendMessage(request_id=uuid4(), content=f"并发消息{index}")
            )
            for index in range(12)
        )
    )
    snapshot = await store.snapshot(principal, pid)
    messages = snapshot["messages"]
    assert len(messages) == 13
    assert sorted(m["sequence"] for m in messages) == list(range(1, 14))
    assert all(
        m["sender_id"] == str(principal.account.id)
        for m in messages
        if m["sender_type"] == "customer"
    )


@pytest.mark.skipif(os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="local PG opt-in")
async def test_human_migration_preserves_historical_content_and_real_authors(postgres):
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    database, schema = postgres
    customer, admin, pid = uuid4(), uuid4(), uuid4()
    customer_run, admin_run = uuid4(), uuid4()

    def migrate(connection):
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for step in reversed(list(scripts.walk_revisions())):
                if step.revision == "0020_human_conversation":
                    for uid, role in ((customer, "user"), (admin, "admin")):
                        connection.execute(
                            text(
                                "INSERT INTO users "
                                "VALUES (:id,:name,:name,'unused',:role,true,now())"
                            ),
                            {"id": uid, "name": role, "role": role},
                        )
                    connection.execute(
                        text(
                            "INSERT INTO delivery_projects "
                            "(id,owner_id,title,brief,stack,revision,created_at,version) "
                            "VALUES (:id,:owner,'迁移验证','','待确认',0,now(),1)"
                        ),
                        {"id": pid, "owner": customer},
                    )
                    for rid, actor in ((customer_run, customer), (admin_run, admin)):
                        connection.execute(
                            text(
                                "INSERT INTO project_runs (id,project_id,actor_id,request_id,"
                                "prompt,status,events,answer,created_at,options,cache_version) "
                                "VALUES (:id,:pid,:actor,:req,'原始输入','completed','[]',"
                                "'已回复',now(),'{}',1)"
                            ),
                            {"id": rid, "pid": pid, "actor": actor, "req": uuid4()},
                        )
                        for role, content in (
                            ("user", "客户原话" if actor == customer else "客服建议"),
                            ("assistant", "AI历史回复"),
                        ):
                            connection.execute(
                                text(
                                    "INSERT INTO project_messages "
                                    "(id,project_id,role,content,created_at,run_id) "
                                    "VALUES (:id,:pid,:role,:content,now(),:run)"
                                ),
                                {
                                    "id": uuid4(),
                                    "pid": pid,
                                    "role": role,
                                    "content": content,
                                    "run": rid,
                                },
                            )
                step.module.upgrade()
        rows = (
            connection.execute(
                text(
                    "SELECT sequence,sender_type,sender_id,role,content "
                    "FROM project_messages ORDER BY sequence"
                )
            )
            .mappings()
            .all()
        )
        assert [r["sequence"] for r in rows] == [1, 2, 3, 4]
        assert sorted(r["content"] for r in rows) == sorted(
            ["客户原话", "客服建议", "AI历史回复", "AI历史回复"]
        )
        support = next(r for r in rows if r["content"] == "客服建议")
        assert (
            support["sender_type"] == "support"
            and support["sender_id"] == admin
            and support["role"] == "assistant"
        )
        assert connection.scalar(text("SELECT message_seq FROM delivery_projects")) == 4
        latest = scripts.get_revision("head").module
        with Operations.context(context):
            latest.downgrade()
            latest.upgrade()
        assert connection.scalar(text("SELECT count(*) FROM project_messages")) == 4

    async with database.engine.begin() as connection:
        await connection.run_sync(migrate)

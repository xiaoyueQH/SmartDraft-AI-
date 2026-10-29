"""Adversarial handover checks; PostgreSQL cases use disposable schemas."""

# ruff: noqa: F811
import asyncio
import json
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_auth import PASSWORD, auth_api, headers, login  # noqa: F401
from test_human_conversation import control, send_chat, state
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import project, wait_run

from backend.agents.runtime import AgentRuntime
from backend.config import AgentSettings, RecoverySettings
from backend.db.conversation_store import ConversationStore
from backend.db.project_store import now
from backend.models.projects import (
    ConversationAction,
    Project,
    ProjectError,
    ProjectMessage,
    ProjectRun,
    SendMessage,
    StartConsultation,
)
from backend.services.flow_guard import FlowGuard

pg_only = pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="local PG opt-in"
)


async def principal(service, name):
    token = await service.login(name, "Postgres-test-password")
    return await service.authenticate(token.access_token)


async def action(store, actor, pid, name, **kwargs):
    snapshot = await store.snapshot(actor, pid)
    return await store.action(
        actor,
        pid,
        ConversationAction(
            request_id=uuid4(),
            expected_version=snapshot["conversation"]["version"],
            action=name,
            **kwargs,
        ),
    )


@pg_only
async def test_parallel_admin_takeover_has_one_winner(postgres_auth):
    service = postgres_auth
    for name in ("support_a", "support_b"):
        await service.create_user(name, name, "admin", "Postgres-test-password")
    customer, first, second = await asyncio.gather(
        *(principal(service, name) for name in ("alice", "support_a", "support_b"))
    )
    store = ConversationStore(service.store.database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    await action(store, customer, pid, "request_human")
    version = (await store.snapshot(customer, pid))["conversation"]["version"]
    results = await asyncio.gather(
        *(
            store.action(
                staff,
                pid,
                ConversationAction(request_id=uuid4(), action="takeover", expected_version=version),
            )
            for staff in (first, second)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    failure = next(result for result in results if isinstance(result, Exception))
    assert isinstance(failure, ProjectError) and failure.status == 409
    snapshot = await store.snapshot(customer, pid)
    winner = snapshot["conversation"]["support_id"]
    loser = next(staff for staff in (first, second) if str(staff.account.id) != winner)
    with pytest.raises(ProjectError):
        await store.send_chat(loser, pid, SendMessage(request_id=uuid4(), content="不能抢答"))
    assert sum("已接受会话" in m["content"] for m in snapshot["messages"]) == 1


@pg_only
async def test_parallel_retries_and_customer_isolation(postgres_auth):
    service = postgres_auth
    first, second = await asyncio.gather(principal(service, "alice"), principal(service, "bob"))
    store = ConversationStore(service.store.database)
    records = await asyncio.gather(
        *(
            store.start_consultation(actor, StartConsultation(request_id=uuid4()))
            for actor in (first, second)
        )
    )
    ids = [UUID(record["id"]) for record in records]
    await asyncio.gather(
        *(
            action(store, actor, pid, "request_human")
            for actor, pid in zip((first, second), ids, strict=True)
        )
    )
    # Identical request IDs across accounts must remain isolated; each account retries concurrently.
    rid = uuid4()
    inputs = [
        SendMessage(request_id=rid, content="甲客户：店员不可看金额"),
        SendMessage(request_id=rid, content="乙客户：只管设备库存"),
    ]
    results = await asyncio.gather(
        *(
            store.send_chat(actor, pid, data)
            for actor, pid, data in zip((first, second), ids, inputs, strict=True)
            for _ in range(6)
        )
    )
    assert len({result["id"] for result in results}) == 2
    for index, (actor, pid) in enumerate(zip((first, second), ids, strict=True)):
        snapshot = await store.snapshot(actor, pid)
        originals = [m for m in snapshot["messages"] if m["sender_type"] == "customer"]
        assert len(originals) == 1 and originals[0]["content"] == inputs[index].content
        assert "login_session_id" not in originals[0]
        with pytest.raises(ProjectError) as error:
            await store.snapshot(actor, ids[1 - index])
        assert error.value.status == 404
        history = await store.customer_history(actor, q="客户", sender="customer")
        assert len(history["items"]) == 1 and history["items"][0]["project_id"] == str(pid)
        with pytest.raises(ProjectError) as error:
            await store.context(actor, ids[1 - index], UUID(originals[0]["id"]))
        assert error.value.status == 404


@pg_only
async def test_human_only_and_expiration_race_converges_to_explicit_choice(postgres_auth):
    service = postgres_auth
    customer = await principal(service, "alice")
    database = service.store.database
    store = ConversationStore(database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    await action(store, customer, pid, "request_human")
    async with database.session() as session:
        row = await session.get(Project, pid)
        row.support_wait_since = now() - timedelta(minutes=4)
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        await asyncio.gather(
            guard.reconcile(pid),
            store.send_chat(
                customer, pid, SendMessage(request_id=uuid4(), content="只等人工，不要AI")
            ),
        )
        await guard.reconcile(pid)
        snapshot = await store.snapshot(customer, pid)
        assert snapshot["conversation"]["mode"] == "human"
        assert snapshot["conversation"]["human_only"]
        async with database.session() as session:
            row = await session.get(Project, pid)
            assert row.auto_reply_message_id is None
    finally:
        await agent.close()


@pg_only
async def test_staff_reply_and_expiration_race_never_duplicates_or_impersonates(postgres_auth):
    service = postgres_auth
    await service.create_user("support", "Support", "admin", "Postgres-test-password")
    customer, staff = await asyncio.gather(
        principal(service, "alice"), principal(service, "support")
    )
    database = service.store.database
    store = ConversationStore(database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    await action(store, staff, pid, "takeover")
    receipt = await store.send_chat(
        customer, pid, SendMessage(request_id=uuid4(), content="帮我确认预约规则")
    )
    async with database.session() as session:
        row = await session.get(Project, pid)
        row.support_wait_since = now() - timedelta(minutes=6)
        msg = await session.get(ProjectMessage, UUID(receipt["message"]["id"]))
        msg.created_at = now() - timedelta(minutes=6)
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        result = await asyncio.gather(
            guard.reconcile(pid),
            store.send_chat(staff, pid, SendMessage(request_id=uuid4(), content="人工确认")),
            return_exceptions=True,
        )
        assert result[0] is None
        assert isinstance(result[1], ProjectError) and result[1].status == 409
        snapshot = await store.snapshot(customer, pid)
        assert snapshot["conversation"]["mode"] == "ai"
        assert not any(m["sender_type"] == "support" for m in snapshot["messages"])
        assert len({m["sequence"] for m in snapshot["messages"]}) == len(snapshot["messages"])
    finally:
        await agent.close()


@pg_only
async def test_recovery_update_cannot_deadlock_customer_waiting_for_project(
    postgres_auth, monkeypatch
):
    import backend.db.project_store as module

    service = postgres_auth
    customer = await principal(service, "alice")
    await service.create_user("support", "Support", "admin", "Postgres-test-password")
    staff = await principal(service, "support")
    database = service.store.database
    store = ConversationStore(database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    await action(store, customer, pid, "request_human")
    identity_locked = asyncio.Event()
    original = module.lock_identity

    async def observed(*args, **kwargs):
        result = await original(*args, **kwargs)
        identity_locked.set()
        return result

    monkeypatch.setattr(module, "lock_identity", observed)
    pending = None
    try:
        # The handover transaction owns the project while the target staff's request
        # owns its identity and waits for this project. Assignment's actual user FK
        # must be checked, so the former full UPDATE identity lock forms a cycle here.
        async with database.session() as session:
            row = await session.scalar(select(Project).where(Project.id == pid).with_for_update())
            pending = asyncio.create_task(store.snapshot(staff, pid))
            await asyncio.wait_for(identity_locked.wait(), 2)
            row.support_id = staff.account.id
            await asyncio.wait_for(session.flush(), 3)
        result = await asyncio.wait_for(pending, 3)
        assert result["conversation"]["support_id"] == str(staff.account.id)
        assert result["conversation"]["mode"] == "human"
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pg_only
async def test_competing_recovery_dispatch_creates_one_run_for_original_message(
    postgres_auth, monkeypatch
):
    service = postgres_auth
    customer = await principal(service, "alice")
    database = service.store.database
    store = ConversationStore(database)
    record = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    pid = UUID(record["id"])
    await store.send_chat(
        customer, pid, SendMessage(request_id=uuid4(), content="请转人工。如何查看我的需求？")
    )
    async with database.session() as session:
        row = await session.get(Project, pid)
        row.support_wait_since = now() - timedelta(minutes=4)
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    launched = []

    def launch(self, store, actor, project_id, run_id, content):
        launched.append((project_id, run_id, actor.session_id))
        self.release()

    monkeypatch.setattr(AgentRuntime, "launch", launch)
    agents = [AgentRuntime(AgentSettings()), AgentRuntime(AgentSettings())]
    guards = [FlowGuard(database, None, agent, RecoverySettings()) for agent in agents]
    try:
        await guards[0].reconcile(pid)
        await asyncio.gather(*(guard.dispatch(pid) for guard in guards))
        await asyncio.gather(*(guard.dispatch(pid) for guard in guards))
        assert len(launched) == 1 and launched[0][2] == customer.session_id
        async with database.session() as session:
            runs = list(
                await session.scalars(select(ProjectRun).where(ProjectRun.project_id == pid))
            )
            messages = list(
                await session.scalars(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == pid, ProjectMessage.sender_type == "customer"
                    )
                )
            )
            assert len(runs) == len(messages) == 1 and messages[0].run_id == runs[0].id
    finally:
        for agent in agents:
            await agent.close()


async def test_new_human_messages_during_ai_handover_remain_unanswered(auth_api, monkeypatch):
    client, _, database = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    record = await project(client, customer)
    pid = record["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        entered.set()
        await release.wait()
        return {"content": "旧问题的回答已保存。"}, {}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    started = await send_chat(client, customer, pid, "先解释一下流程，暂时不生成")
    await asyncio.wait_for(entered.wait(), 3)
    try:
        waiting = await control(client, staff, pid, "takeover")
        assert waiting["mode"] == "pending"
        queued = await send_chat(client, customer, pid, "补充：人工请确认店员不能查看金额")
        pending = await state(client, customer, pid)
        assert pending["conversation"]["mode"] == "pending"
    finally:
        release.set()
    assert (await wait_run(client, customer, pid, started["id"]))["status"] == "completed"
    snapshot = await state(client, customer, pid)
    assert snapshot["conversation"]["mode"] == "human"
    async with database.session() as session:
        row = await session.get(Project, UUID(pid))
        message = await session.get(ProjectMessage, UUID(queued["message"]["id"]))
        assert row.answered_through < message.sequence and message.run_id is None
        assert now() - ConversationStore.utc(row.support_wait_since) < timedelta(seconds=5)
    await send_chat(client, staff, pid, "收到，将按这个权限要求整理。")
    await control(client, staff, pid, "release")
    async with database.session() as session:
        row = await session.get(Project, UUID(pid))
        assert row.auto_reply_message_id is None


async def test_other_login_cannot_reuse_session_header(auth_api):
    client, _, _ = auth_api
    alice, bob = await login(client), await login(client, "bob")
    identity = {**headers(alice), "X-Session-ID": bob["session_id"]}
    assert (await client.get("/api/auth/me", headers=identity)).status_code == 401
    assert (await client.get("/api/projects", headers=identity)).status_code == 401


async def test_request_replay_after_network_loss_does_not_start_another_ai_run(
    auth_api, monkeypatch
):
    client, _, database = auth_api
    identity = headers(await login(client))
    record = await project(client, identity)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"content": "恢复后仍是同一次回答。"}, {}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    data = {"request_id": str(uuid4()), "content": "先讨论流程，不生成"}
    endpoint = f"/api/projects/{record['id']}/messages"
    first = await client.post(endpoint, headers=identity, json=data)
    await asyncio.wait_for(entered.wait(), 3)
    try:
        replay = await client.post(endpoint, headers=identity, json=data)
        assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
        assert (
            await client.get(f"/api/projects/{record['id']}", headers=identity)
        ).status_code == 200
    finally:
        release.set()
    await wait_run(client, identity, record["id"], first.json()["id"])
    again = await client.post(endpoint, headers=identity, json=data)
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert calls == 1
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(ProjectMessage).where(ProjectMessage.project_id == UUID(record["id"]))
            )
        )
        assert len(rows) == 2 and sum(row.sender_type == "customer" for row in rows) == 1


async def test_logout_of_one_customer_does_not_break_another_customer(auth_api):
    client, _, _ = auth_api
    first, second = await login(client), await login(client, "bob")
    one, two = await asyncio.gather(
        project(client, headers(first)), project(client, headers(second))
    )
    assert (await client.post("/api/auth/logout", headers=headers(first))).status_code == 204
    assert (
        await client.get(f"/api/projects/{one['id']}", headers=headers(first))
    ).status_code == 401
    assert (
        await client.get(f"/api/projects/{two['id']}", headers=headers(second))
    ).status_code == 200
    assert (
        await client.get(f"/api/projects/{one['id']}", headers=headers(second))
    ).status_code == 404


@pg_only
async def test_parallel_agent_graphs_keep_messages_previews_and_threads_isolated(
    postgres_auth, monkeypatch
):
    service = postgres_auth
    first, second = await asyncio.gather(principal(service, "alice"), principal(service, "bob"))
    store = ConversationStore(service.store.database)
    records = await asyncio.gather(
        *(
            store.start_consultation(actor, StartConsultation(request_id=uuid4()))
            for actor in (first, second)
        )
    )
    ids = [UUID(record["id"]) for record in records]
    prompts = ["甲客户独有标识A，先聊天不要生成", "乙客户独有标识B，先聊天不要生成"]
    both_entered, release = asyncio.Event(), asyncio.Event()
    entered = 0

    async def complete(self, messages, **kwargs):
        nonlocal entered
        data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
        tag = "甲客户独有标识A" if "甲客户独有标识A" in data["input"] else "乙客户独有标识B"
        entered += 1
        if entered == 2:
            both_entered.set()
        await kwargs["on_text"](tag)
        await release.wait()
        return {"content": tag + "的独立回答"}, {}

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    agent = AgentRuntime(AgentSettings())
    runs = []
    try:
        for actor, pid, text in zip((first, second), ids, prompts, strict=True):
            run, created = await store.start_run(
                actor, pid, SendMessage(request_id=uuid4(), content=text)
            )
            assert created
            runs.append(UUID(run["id"]))
            agent.reserve()
            agent.launch(store, actor, pid, runs[-1], text)
        await asyncio.wait_for(both_entered.wait(), 4)
        for index, rid in enumerate(runs):
            preview = await agent.preview(store, rid)
            assert prompts[index].split("，")[0] == preview["text"]
        release.set()
        await asyncio.wait_for(asyncio.gather(*agent.tasks), 6)
        threads = []
        for index, (actor, pid, rid) in enumerate(zip((first, second), ids, runs, strict=True)):
            result = await store.run(actor, pid, rid)
            assert result["status"] == "completed"
            snapshot = await store.detail(actor, pid)
            content = "\n".join(message["content"] for message in snapshot["messages"])
            assert prompts[index].split("，")[0] in content
            assert prompts[1 - index].split("，")[0] not in content
            threads.append(snapshot["project"]["consultation"]["checkpoint_thread"])
        assert len(set(threads)) == 2 and agent.active == 0
    finally:
        release.set()
        await agent.close()


async def test_handover_during_document_writing_preserves_generated_document(auth_api, monkeypatch):
    from drafting_support import DraftModel
    from test_consultation import install_model, send

    client, _, _ = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    record = await project(client, customer)
    pid = record["id"]
    model = DraftModel()
    install_model(monkeypatch, model)
    await send(client, customer, pid, "先记录业务信息：手工登记和查询预约。")
    writing, release = asyncio.Event(), asyncio.Event()

    async def complete(self, messages, **kwargs):
        if "DRAFT_WRITE" in messages[0]["content"]:
            writing.set()
            await release.wait()
        return await model.respond(messages)

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    response = await client.post(
        f"/api/projects/{pid}/messages",
        headers=customer,
        json={"request_id": str(uuid4()), "content": "生成一份草案", "intent": "draft"},
    )
    assert response.status_code == 202
    await asyncio.wait_for(writing.wait(), 4)
    try:
        requested = await control(client, customer, pid, "request_human")
        assigned = await control(client, staff, pid, "takeover")
        assert requested["mode"] == assigned["mode"] == "pending"
        busy = await client.post(
            f"/api/projects/{pid}/messages",
            headers=staff,
            json={"request_id": str(uuid4()), "content": "人工不可提前回复"},
        )
        assert busy.status_code == 409
    finally:
        release.set()
    completed = await wait_run(client, customer, pid, response.json()["id"])
    assert completed["status"] == "completed"
    snapshot = await state(client, customer, pid)
    assert snapshot["conversation"]["mode"] == "human"
    detail = (await client.get(f"/api/projects/{pid}", headers=customer)).json()
    assert len(detail["items"]) == 1
    document = (
        await client.get(f"/api/projects/{pid}/items/{detail['items'][0]['id']}", headers=customer)
    ).json()
    assert "根据已保存信息组织登记" in document["content"]
    assert "详细说明未生成" not in document["content"]

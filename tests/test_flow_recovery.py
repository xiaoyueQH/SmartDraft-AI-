# ruff: noqa: F811
import asyncio
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_human_conversation import control, send_chat
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import act, draft, project, wait_run

from backend.agents.runtime import AgentRuntime
from backend.config import AgentSettings, RecoverySettings
from backend.db.conversation_store import ConversationStore
from backend.db.project_store import append_message, now
from backend.models.orm import LoginSession
from backend.models.projects import (
    ConversationAction,
    Project,
    ProjectItem,
    ProjectMessage,
    ProjectRun,
    SendMessage,
    StartConsultation,
    human_only_requested,
)
from backend.services.flow_guard import FlowGuard


@pytest.mark.parametrize(
    "text", ["只要人工，不要AI", "我不要AI回复", "这次只要人工客服", "只等人工"]
)
def test_explicit_human_only_choice(text):
    assert human_only_requested(text)


@pytest.mark.parametrize(
    "text", ["我们的业务系统不要AI审核功能", "举个例子：不要AI", "用户说不要AI怎么办"]
)
def test_product_requirements_and_quotes_do_not_change_reception(text):
    assert not human_only_requested(text)


async def test_revoked_login_does_not_restart_pending_ai_reply(auth_api, monkeypatch):
    client, _, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    p = await project(client, identity)
    await send_chat(client, identity, p["id"], "请转人工。我想了解现有方案？")
    await expire_wait(database, p["id"])
    async with database.session() as session:
        row = await session.get(LoginSession, UUID(tokens["session_id"]))
        row.revoked_at = now()
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    agent = AgentRuntime(AgentSettings())
    try:
        await FlowGuard(database, None, agent, RecoverySettings()).tick()
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.auto_reply_message_id is None and row.support_notice == "resume_required"
            assert not await session.scalar(
                select(ProjectRun.id).where(ProjectRun.project_id == row.id)
            )
    finally:
        await agent.close()


async def expire_wait(database, pid):
    async with database.session() as session:
        row = await session.get(Project, UUID(str(pid)))
        row.support_wait_since = now() - timedelta(minutes=4)


@pytest.mark.parametrize("method", ["fail", "checkpoint_ready"])
@pytest.mark.parametrize("committed", [False, True])
async def test_terminal_paths_require_actual_saved_receipt(auth_api, method, committed):
    client, service, database = auth_api
    tokens = await login(client)
    principal = await service.authenticate(tokens["access_token"])
    p = await project(client, headers(tokens))
    pid = UUID(p["id"])
    store = ConversationStore(database)
    run, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content="先讨论，不生成")
    )
    rid = UUID(run["id"])
    async with database.session() as session:
        row = await session.get(Project, pid)
        task = await session.get(ProjectRun, rid)
        task.answer = "已保存的回答片段"
        append_message(session, row, task.answer, "ai", run_id=rid)
        if committed:
            row.consultation = {"last_run_id": str(rid)}
    if method == "fail":
        await store.fail(rid, "服务中断")
    else:
        await store.checkpoint_ready(rid)
    result = await store.run(principal, pid, rid)
    assert result["status"] == ("completed" if committed else "failed")
    assert "已保存的回答片段" in result["answer"]


async def test_timeout_replies_to_original_message_once_without_duplicate_input(
    auth_api, monkeypatch
):
    client, _, database = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    original = await send_chat(client, identity, p["id"], "请转人工。我想问如何查看已有需求？")
    await expire_wait(database, p["id"])
    calls = 0

    async def complete(self, messages, **kwargs):
        nonlocal calls
        calls += 1
        return {"content": "可以打开需求草案查看已保存的内容。"}, {"total_tokens": 20}

    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    monkeypatch.setattr(AgentRuntime, "complete", complete)
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        await guard.tick()
        async with database.session() as session:
            row = await session.scalar(
                select(ProjectRun).where(ProjectRun.project_id == UUID(p["id"]))
            )
            rid = str(row.id)
        result = await wait_run(client, identity, p["id"], rid)
        assert result["status"] == "completed"
        await guard.tick()
        async with database.session() as session:
            messages = list(
                await session.scalars(
                    select(ProjectMessage).where(ProjectMessage.project_id == UUID(p["id"]))
                )
            )
            inputs = [m for m in messages if m.sender_type == "customer"]
            assert len(inputs) == 1 and str(inputs[0].id) == original["message"]["id"]
            assert sum(m.sender_type == "ai" for m in messages) == 1
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "ai" and row.support_id is None
            assert row.auto_reply_message_id is None
            assert any("繁忙" in m.content for m in messages if m.sender_type == "system")
        assert calls == 1
    finally:
        await guard.close()
        await agent.close()


async def test_human_only_wait_does_not_expire_and_control_message_does_not_call_model(auth_api):
    client, _, database = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    await send_chat(client, identity, p["id"], "只要人工，不要AI")
    await expire_wait(database, p["id"])
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        await guard.tick()
        await guard.tick()
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.human_only and row.conversation_mode == "human"
            warnings = await session.scalar(
                select(func.count())
                .select_from(ProjectMessage)
                .where(
                    ProjectMessage.project_id == row.id,
                    ProjectMessage.content.contains("已保留您只需人工"),
                )
            )
            assert warnings == 1
            assert not await session.scalar(
                select(ProjectRun.id).where(ProjectRun.project_id == row.id)
            )
        await control(client, identity, p["id"], "resume_ai")
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert not row.human_only and row.conversation_mode == "ai"
    finally:
        await agent.close()


async def test_wait_clock_starts_after_existing_operation_finishes(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    principal, identity = await service.authenticate(tokens["access_token"]), headers(tokens)
    p = await project(client, identity)
    store = ConversationStore(database)
    run, _ = await store.start_run(
        principal, UUID(p["id"]), SendMessage(request_id=uuid4(), content="先讨论业务")
    )
    await control(client, identity, p["id"], "request_human")
    await expire_wait(database, p["id"])
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        await guard.reconcile(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "pending"
            running = await session.get(ProjectRun, UUID(run["id"]))
            running.status = "completed"
        await guard.reconcile(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "human"
            assert now() - store.utc(row.support_wait_since) < timedelta(seconds=5)
    finally:
        await agent.close()


async def test_idle_extension_expires_but_active_extension_cannot_be_spammed(auth_api):
    client, _, database = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    claimed = await control(client, admin, p["id"], "takeover")
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_activity_at = now() - timedelta(minutes=14, seconds=30)
    result = await control(client, admin, p["id"], "extend")
    assert result["extended_until"]
    blocked = await client.post(
        f"/api/projects/{p['id']}/conversation/actions",
        headers=admin,
        json={
            "request_id": str(uuid4()),
            "action": "extend",
            "expected_version": result["version"],
        },
    )
    assert blocked.status_code == 409
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            row.support_activity_at = now() - timedelta(minutes=20)
        await guard.reconcile(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "human" and str(row.support_id) == claimed["support_id"]
            row.support_extended_until = now() - timedelta(seconds=1)
        await guard.reconcile(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "ai" and row.auto_reply_message_id is None
    finally:
        await agent.close()


async def test_customer_followups_do_not_reset_unanswered_staff_deadline(auth_api):
    client, _, database = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, admin, p["id"], "takeover")
    first = await send_chat(client, customer, p["id"], "我的订单如何处理？")
    await send_chat(client, customer, p["id"], "还在吗？")
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_wait_since = now() - timedelta(minutes=10)
        row.support_activity_at = now()
        message = await session.get(ProjectMessage, UUID(first["message"]["id"]))
        message.created_at = now() - timedelta(minutes=6)
    agent = AgentRuntime(AgentSettings())
    try:
        await FlowGuard(database, None, agent, RecoverySettings()).reconcile(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, UUID(p["id"]))
            assert row.conversation_mode == "ai"
    finally:
        await agent.close()


async def test_stalled_partial_answer_is_not_misreported_as_completed_and_review_reminder_is_once(
    auth_api,
):
    client, service, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    p = await project(client, identity)
    doc = await draft(client, identity, p["id"])
    submitted = await act(client, identity, doc, "submit")
    principal = await service.authenticate(tokens["access_token"])
    store = ConversationStore(database)
    run, _ = await store.start_run(
        principal, UUID(p["id"]), SendMessage(request_id=uuid4(), content="生成需求")
    )
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.consultation = {"discussion_run_id": run["id"]}
        task = await session.get(ProjectRun, UUID(run["id"]))
        task.created_at = now() - timedelta(minutes=4)
        task.answer = "本轮沟通已保存，正在生成需求。"
        append_message(session, row, task.answer, "ai", run_id=task.id)
        item = await session.get(ProjectItem, UUID(doc["id"]))
        item.workflow_activity_at = now() - timedelta(days=2)
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings())
    try:
        # A direct run lookup must follow the same recovery rule as the background scan.
        response = await client.get(f"/api/projects/{p['id']}/runs/{run['id']}", headers=identity)
        assert response.status_code == 200 and response.json()["status"] == "failed"
        await guard.tick()
        await guard.tick()
        async with database.session() as session:
            assert (await session.get(ProjectRun, UUID(run["id"]))).status == "failed"
            item = await session.get(ProjectItem, UUID(doc["id"]))
            assert item.status == "submitted" and item.version == submitted["version"]
            assert not item.customer_confirmed and not item.staff_confirmed
            warnings = await session.scalar(
                select(func.count())
                .select_from(ProjectMessage)
                .where(
                    ProjectMessage.project_id == UUID(p["id"]),
                    ProjectMessage.content.contains("仍待处理或确认"),
                )
            )
            assert warnings == 1 and item.content == doc["content"]
    finally:
        await agent.close()


async def test_background_guard_works_without_browser_and_after_new_guard_instance(auth_api):
    client, _, database = auth_api
    identity = headers(await login(client))
    p = await project(client, identity)
    await send_chat(client, identity, p["id"], "转人工")
    await expire_wait(database, p["id"])
    agent = AgentRuntime(AgentSettings())
    guard = FlowGuard(database, None, agent, RecoverySettings(scan_seconds=1))
    try:
        guard.start()
        for _ in range(30):
            await asyncio.sleep(0.02)
            async with database.session() as session:
                row = await session.get(Project, UUID(p["id"]))
                if row.conversation_mode == "ai":
                    break
        assert row.conversation_mode == "ai"
        await guard.close()
        await FlowGuard(database, None, agent, RecoverySettings()).tick()
        async with database.session() as session:
            assert not await session.scalar(
                select(ProjectRun.id).where(ProjectRun.project_id == UUID(p["id"]))
            )
    finally:
        await guard.close()
        await agent.close()


@pytest.mark.skipif(os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="local PG opt-in")
async def test_real_postgres_two_guards_cannot_duplicate_timeout_transition(postgres_auth):
    service = postgres_auth
    bundle = await service.login("alice", "Postgres-test-password")
    principal = await service.authenticate(bundle.access_token)
    database = service.store.database
    store = ConversationStore(database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    await store.action(
        principal,
        pid,
        ConversationAction(request_id=uuid4(), action="request_human", expected_version=1),
    )
    await expire_wait(database, pid)
    a, b = AgentRuntime(AgentSettings()), AgentRuntime(AgentSettings())
    try:
        guards = [FlowGuard(database, None, agent, RecoverySettings()) for agent in (a, b)]
        await asyncio.gather(*(guard.reconcile(pid) for guard in guards))
        async with database.session() as session:
            messages = list(
                await session.scalars(
                    select(ProjectMessage).where(ProjectMessage.project_id == pid)
                )
            )
            assert sum("暂时无人接待" in row.content for row in messages) == 1
            assert len({row.sequence for row in messages}) == len(messages)
    finally:
        await a.close()
        await b.close()

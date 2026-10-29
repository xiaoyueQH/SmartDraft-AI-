# ruff: noqa: F811
"""Behavioral checks for bounded reads, recoverable state and source attribution."""

import hashlib
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import draft, project

from backend.agents.consultation.state import verified_patch
from backend.agents.runtime import AgentRuntime
from backend.config import AgentSettings
from backend.db.checkpoints import DatabaseCheckpointer
from backend.db.project_store import ProjectStore, now
from backend.db.review_store import ReviewStore
from backend.models.consultation import ConsultationState, Extraction, FactEvidence
from backend.models.projects import (
    ProjectMessage,
    ProjectOperation,
    ProjectRun,
    SendMessage,
    StartConsultation,
)
from backend.models.review import DocumentReview, StartReview


def test_source_positions_are_server_computed_unicode_character_offsets():
    prompt = "开头😀" + "甲" * 498 + "客户只要预约，不要会员。"
    extraction = Extraction.model_validate(
        {
            "notes": {"scope": ["预约"]},
            "sources": [
                {
                    "field": "scope",
                    "value": "预约",
                    "source_id": "current:1",
                    "message_sha256": "forged",
                    "quote_start": 0,
                }
            ],
            "reply": "已记录",
        }
    )
    _, sources = verified_patch(extraction, prompt, "run", {})
    source = sources[0]
    assert source.quote_start == 500
    assert prompt[source.quote_start : source.quote_end] == source.quote
    assert source.message_sha256 == hashlib.sha256(prompt.encode()).hexdigest()
    assert source.quote_sha256 == hashlib.sha256(source.quote.encode()).hexdigest()


def test_quote_whitespace_survives_checkpoint_and_database_validation():
    prompt = "\n  客户需要预约。\n"
    extraction = Extraction.model_validate(
        {
            "notes": {"scope": ["预约"]},
            "reply": "已记录",
            "sources": [{"field": "scope", "value": "预约", "source_id": "current:0"}],
        }
    )
    _, sources = verified_patch(extraction, prompt, "run", {})
    saved = ConsultationState.model_validate(
        ConsultationState(sources=sources).model_dump(mode="json")
    )
    assert saved.sources[0].quote == prompt
    assert prompt[saved.sources[0].quote_start : saved.sources[0].quote_end] == prompt
    assert saved.sources[0].quote_sha256 == hashlib.sha256(prompt.encode()).hexdigest()


async def test_project_summary_omits_full_documents_and_traces_but_detail_is_authorized(auth_api):
    client, service, database = auth_api
    alice, bob = await login(client), await login(client, "bob")
    identity = headers(alice)
    p = await project(client, identity)
    text = "文档完整内容。" * 5000
    item = await draft(client, identity, p["id"], content=text)
    async with database.session() as session:
        session.add(
            ProjectRun(
                id=uuid4(),
                project_id=UUID(p["id"]),
                actor_id=UUID(alice["user"]["id"]),
                request_id=uuid4(),
                prompt="原始问题" * 1000,
                answer="内部回复" * 4000,
                status="completed",
                options={},
                events=[{"summary": "详细事件" * 1000}],
                created_at=now(),
            )
        )
    response = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert response["items"][0]["content_loaded"] is False
    assert len(response["items"][0]["content"]) == 140
    assert response["runs"][0]["events"] == []
    assert response["runs"][0]["prompt"] == response["runs"][0]["answer"] == ""
    page = (await client.get(f"/api/projects/{p['id']}/items", headers=identity)).json()
    assert page["items"][0]["content_loaded"] is False
    path = f"/api/projects/{p['id']}/items/{item['id']}"
    assert (await client.get(path, headers=identity)).json()["content"] == text
    assert (await client.get(path, headers=headers(bob))).status_code == 404


async def test_fact_evidence_outlives_working_sources_and_detects_changed_original(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    principal = await service.authenticate(tokens["access_token"])
    p = await project(client, identity)
    store = ProjectStore(database)
    pid = UUID(p["id"])
    for turn in range(6):
        values = [f"功能{index}" for index in range(20)]
        prompt = f"第{turn}轮再次确认：" + "、".join(values)
        run, created = await store.start_run(
            principal, pid, SendMessage(request_id=uuid4(), content=prompt)
        )
        assert created
        extraction = Extraction.model_validate(
            {
                "notes": {"scope": values},
                "reply": "已记录",
                "sources": [
                    {"field": "scope", "value": value, "source_id": "current:0"} for value in values
                ],
            }
        )
        patch, sources = verified_patch(extraction, prompt, run["id"], {})
        snapshot = await store.detail(principal, pid)
        await store.finish(
            principal,
            pid,
            UUID(run["id"]),
            0,
            "已记录",
            [],
            [patch],
            consultation=ConsultationState(sources=sources).model_dump(mode="json"),
            expected_version=snapshot["project"]["version"],
        )
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(FactEvidence)) == 120
        old = await session.scalar(select(FactEvidence).order_by(FactEvidence.created_at).limit(1))
        evidence_id, run_id = old.id, UUID(old.source["run_id"])
    path = f"/api/projects/{pid}/fact-sources/{evidence_id}"
    assert (await client.get(path, headers=identity)).json()["verified"] is True
    bob = headers(await login(client, "bob"))
    assert (await client.get(path, headers=bob)).status_code == 404
    async with database.session() as session:
        original = await session.get(ProjectRun, run_id)
        original.prompt = "已被修改的原话"
    assert (await client.get(path, headers=identity)).status_code == 409


async def test_review_can_read_old_sources_and_detect_source_changes(auth_api):
    client, service, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    principal = await service.authenticate(tokens["access_token"])
    p = await project(client, identity)
    item = await draft(client, identity, p["id"])
    first_id = uuid4()
    async with database.session() as session:
        for index in range(85):
            session.add(
                ProjectMessage(
                    id=first_id if index == 0 else uuid4(),
                    project_id=UUID(p["id"]),
                    role="user",
                    content="早期要求：取消后必须释放会议室。"
                    if not index
                    else "其他业务信息。" * 160,
                    created_at=now() - timedelta(minutes=100 - index),
                )
            )
    store = ReviewStore(database)
    job, _ = await store.start(
        principal,
        UUID(p["id"]),
        UUID(item["id"]),
        StartReview(request_id=uuid4(), expected_version=item["version"]),
    )
    source = (
        await store.load(principal, UUID(p["id"]), UUID(item["id"]), UUID(job["id"]), internal=True)
    )["source"]
    assert len(source["source_manifest"]) == 85
    assert sum(len(q) for q in source["client_quotes"]) <= 6000
    sid = f"message:{first_id}"
    result = await store.read_sources(
        principal, UUID(p["id"]), UUID(item["id"]), UUID(job["id"]), query="取消后"
    )
    assert result["sources"][0]["id"] == sid
    async with database.session() as session:
        row = await session.get(ProjectMessage, first_id)
        row.content = "不同的原话"
    with pytest.raises(Exception, match="原话已变化"):
        await store.read_sources(
            principal, UUID(p["id"]), UUID(item["id"]), UUID(job["id"]), ids=[sid]
        )


async def test_cleanup_keeps_waiting_threads_and_removes_only_old_terminal_runs(auth_api):
    client, _, database = auth_api
    tokens = await login(client)
    p = await project(client, headers(tokens))
    pid, actor = UUID(p["id"]), UUID(tokens["user"]["id"])
    completed, running = uuid4(), uuid4()
    async with database.session() as session:
        for rid, status in ((completed, "completed"), (running, "running")):
            session.add(
                ProjectRun(
                    id=rid,
                    project_id=pid,
                    actor_id=actor,
                    request_id=uuid4(),
                    prompt="问题",
                    status=status,
                    events=[],
                    options={},
                    answer="",
                    created_at=now() - timedelta(days=2),
                )
            )
    saver = DatabaseCheckpointer(database, pid)
    for thread in (f"draft:{completed}", f"draft:{running}", f"consultation:{completed}"):
        await saver.aput({"configurable": {"thread_id": thread}}, empty_checkpoint(), {}, {})
    assert await saver.cleanup_terminal() == 1
    assert await saver.aget_tuple({"configurable": {"thread_id": f"draft:{completed}"}}) is None
    assert await saver.aget_tuple({"configurable": {"thread_id": f"draft:{running}"}})
    assert await saver.aget_tuple({"configurable": {"thread_id": f"consultation:{completed}"}})


async def test_cleanup_preserves_ready_review_and_recent_decision_receipt(auth_api):
    client, _, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    p = await project(client, identity)
    item = await draft(client, identity, p["id"])
    pid, actor = UUID(p["id"]), UUID(tokens["user"]["id"])
    ready, old_discarded, just_discarded = uuid4(), uuid4(), uuid4()
    async with database.session() as session:
        for rid, status in (
            (ready, "ready"),
            (old_discarded, "discarded"),
            (just_discarded, "discarded"),
        ):
            session.add(
                DocumentReview(
                    id=rid,
                    project_id=pid,
                    item_id=UUID(item["id"]),
                    actor_id=actor,
                    request_id=uuid4(),
                    fingerprint="a" * 64,
                    status=status,
                    source={},
                    blocks=[],
                    report={},
                    candidate="",
                    events=[],
                    error="",
                    created_at=now() - timedelta(days=3),
                )
            )
        session.add(
            ProjectOperation(
                id=uuid4(),
                project_id=pid,
                actor_id=actor,
                request_id=uuid4(),
                fingerprint="a" * 64,
                action=f"review_discard:{item['id']}",
                result={"review": {"id": str(just_discarded)}},
                created_at=now(),
            )
        )
    saver = DatabaseCheckpointer(database, pid)
    for rid in (ready, old_discarded, just_discarded):
        await saver.aput(
            {"configurable": {"thread_id": f"review:{rid}"}}, empty_checkpoint(), {}, {}
        )
    assert await saver.cleanup_terminal() == 1
    assert await saver.aget_tuple({"configurable": {"thread_id": f"review:{ready}"}})
    assert await saver.aget_tuple({"configurable": {"thread_id": f"review:{just_discarded}"}})
    assert (
        await saver.aget_tuple({"configurable": {"thread_id": f"review:{old_discarded}"}}) is None
    )


@pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_LIVE_MODEL") != "1",
    reason="Explicit opt-in for paid live model check",
)
async def test_live_model_saves_customer_facts_in_isolated_postgres(postgres_auth):
    from backend.config import get_settings

    service = postgres_auth
    tokens = await service.login("alice", "Postgres-test-password")
    principal = await service.authenticate(tokens.access_token)
    store = ProjectStore(service.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    prompt = (
        "我们是一个小型宠物美容店，现在用纸本记录预约，容易重复安排。"
        "只有店主和两名美容师使用，希望客户可以在小程序自己预约。"
        "先记录这些业务信息，不要生成需求书。"
    )
    result, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content=prompt)
    )
    runtime = AgentRuntime(get_settings().agent)
    try:
        runtime.reserve()
        await runtime.execute(store, principal, pid, UUID(result["id"]), prompt)
        run = await store.run(principal, pid, UUID(result["id"]))
        assert run["status"] == "completed", run["answer"]
        snapshot = await store.detail(principal, pid, summaries=True)
        assert snapshot["project"]["discovery"]
        assert not snapshot["items"]
        assert any(event.get("model") == "qwen-flash" for event in run["events"])
        sources = await store.fact_sources(principal, pid, 0, 100)
        assert sources["items"]
        first = await store.fact_source(principal, pid, sources["items"][0]["id"])
        assert first["verified"] and first["content"] == prompt
        thread = snapshot["project"]["consultation"]["checkpoint_thread"]
        checkpoint = await DatabaseCheckpointer(store.database, pid).aget_tuple(
            {"configurable": {"thread_id": thread}}
        )
        payload = checkpoint.checkpoint["channel_values"]["payload"]
        assert payload["drafts"] == [] and payload["extraction"] is None
        assert runtime.active == 0 and runtime.previews == {}
    finally:
        await runtime.close()


async def test_cache_cleanup_failure_still_releases_execution_slot(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runtime = AgentRuntime(AgentSettings())
    runtime.active = 1
    store = SimpleNamespace(
        detail=AsyncMock(side_effect=ValueError()),
        fail=AsyncMock(),
        cache=SimpleNamespace(
            settings=SimpleNamespace(prefix="test"),
            _redis=AsyncMock(side_effect=RuntimeError("cache failed")),
        ),
    )
    monkeypatch.setattr(runtime, "cleanup_checkpoints", AsyncMock())
    try:
        await runtime.execute(store, None, uuid4(), uuid4(), "测试")
        assert runtime.active == 0 and runtime.previews == {}
    finally:
        await runtime.close()

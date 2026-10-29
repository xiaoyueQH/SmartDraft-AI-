# ruff: noqa: F811
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from test_auth import auth_api, headers, login  # noqa: F401
from test_human_conversation import control, send_chat
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import draft, project

from backend.agents.runtime import AgentRuntime
from backend.config import AgentSettings, RecoverySettings
from backend.db.conversation_store import ConversationStore
from backend.db.project_store import ProjectStore, now
from backend.models.projects import Project, ProjectItem, ProjectRun, StartConsultation
from backend.models.review import DocumentReview


async def inbox(client, identity, **params):
    result = await client.get("/api/conversations/requests", headers=identity, params=params)
    assert result.status_code == 200, result.text
    return result.json()


def run(pid, actor, *, status="failed", created=None, silent=False):
    return ProjectRun(
        project_id=UUID(str(pid)),
        actor_id=UUID(str(actor)),
        request_id=uuid4(),
        prompt="private prompt",
        status=status,
        options={"silent": silent},
        events=[],
        answer="private vendor failure",
        created_at=created or now(),
    )


async def test_reason_filter_and_deadline_use_extended_wait_and_human_choice(auth_api):
    client, _, database = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, customer, p["id"], "request_human")
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_wait_since = now() - timedelta(seconds=150)
    result = await inbox(client, staff, reason="expiring")
    assert result["counts"]["total"] == 1 and result["counts"]["expiring"] == 1
    assert set(result["items"][0]["reasons"]) == {"waiting", "expiring"}
    assert result["items"][0]["deadline_at"]
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_extended_until = now() + timedelta(minutes=10)
    assert not (await inbox(client, staff, reason="expiring"))["items"]
    await control(client, customer, p["id"], "wait_human")
    result = await inbox(client, staff, reason="human_only")
    assert result["items"][0]["human_only"] and "expiring" not in result["items"][0]["reasons"]
    assert result["counts"]["waiting"] == 1


async def test_custom_recovery_policy_reaches_inbox_api(auth_api):
    client, _, database = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, customer, p["id"], "request_human")
    app = client._transport.app
    app.state.settings = app.state.settings.model_copy(
        update={
            "recovery": RecoverySettings(
                enabled=False,
                support_wait_seconds=600,
            )
        }
    )
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_wait_since = now() - timedelta(seconds=150)
    result = await inbox(client, staff)
    assert "expiring" not in result["items"][0]["reasons"]
    assert result["items"][0]["deadline_at"] > result["checked_at"]


async def test_custom_recovery_policy_survives_agent_wrapped_store(auth_api, monkeypatch):
    client, service, database = auth_api
    tokens = await login(client)
    p = await project(client, headers(tokens))
    await control(client, headers(tokens), p["id"], "request_human")
    pid = UUID(p["id"])
    async with database.session() as session:
        (await session.get(Project, pid)).support_wait_since = now() - timedelta(minutes=4)
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    agent = AgentRuntime(AgentSettings(), recovery=RecoverySettings(support_wait_seconds=600))
    principal = await service.authenticate(tokens["access_token"])
    store = ProjectStore(database)
    try:
        await agent.settle_conversation(store, pid)
        await agent.try_facts(store, principal, pid)
        async with database.session() as session:
            row = await session.get(Project, pid)
            assert row.conversation_mode == "human" and row.support_notice == ""
    finally:
        await agent.close()


async def test_unanswered_assignment_scope_and_search_are_server_filtered(auth_api):
    client, _, database = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await control(client, staff, p["id"], "takeover")
    await send_chat(client, customer, p["id"], "请确认订单")
    result = await inbox(client, staff, reason="unanswered", q="Alice")
    assert len(result["items"]) == 1 and result["items"][0]["support_name"] == "Admin"
    assert not (await inbox(client, staff, q="missing-customer"))["items"]
    before = result["items"][0]["deadline_at"]
    await send_chat(client, staff, p["id"], "已收到")
    assert not (await inbox(client, staff, reason="unanswered"))["items"]
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        assert row.support_id and row.conversation_mode == "human"
    # The normal assigned idle conversation is not a pending issue once replied.
    assert not (await inbox(client, staff))["items"] and before


async def test_historical_failure_is_hidden_after_new_success_and_read_is_side_effect_free(
    auth_api,
):
    client, _, database = auth_api
    tokens = await login(client)
    customer, staff = headers(tokens), headers(await login(client, "admin"))
    p = await project(client, customer)
    async with database.session() as session:
        session.add(run(p["id"], tokens["user"]["id"]))
    result = await inbox(client, staff, reason="failed")
    assert len(result["items"]) == 1
    assert "private" not in str(result) and "login_session_id" not in str(result)
    async with database.session() as session:
        session.add(
            run(
                p["id"],
                tokens["user"]["id"],
                status="completed",
                silent=True,
                created=now() + timedelta(seconds=1),
            )
        )
    assert len((await inbox(client, staff, reason="failed"))["items"]) == 1
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        old_version, old_sequence = row.conversation_version, row.message_seq
        session.add(
            run(
                p["id"],
                tokens["user"]["id"],
                status="completed",
                created=now() + timedelta(seconds=2),
            )
        )
    assert not (await inbox(client, staff, reason="failed"))["items"]
    async with database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        assert (row.conversation_version, row.message_seq) == (old_version, old_sequence)


async def test_filter_happens_before_pagination_and_deleted_projects_are_excluded(auth_api):
    client, _, database = auth_api
    tokens = await login(client)
    customer, staff = headers(tokens), headers(await login(client, "admin"))
    ids = []
    for index in range(13):
        p = await project(client, customer)
        ids.append(UUID(p["id"]))
        async with database.session() as session:
            row = await session.get(Project, ids[-1])
            row.title = f"接待{index}"
            row.conversation_mode = "human"
            row.support_wait_since = now()
            if index == 12:
                row.extraction_error = "private failure"
    selected = await inbox(client, staff, reason="failed", limit=1)
    assert len(selected["items"]) == 1 and selected["items"][0]["id"] == str(ids[-1])
    assert selected["counts"]["total"] == 13 and not selected["has_more"]
    first = await inbox(client, staff, limit=10)
    second = await inbox(client, staff, limit=10, offset=10)
    assert len(first["items"]) == 10 and first["has_more"] and len(second["items"]) == 3
    assert len({row["id"] for row in first["items"] + second["items"]}) == 13
    async with database.session() as session:
        row = await session.get(Project, ids[-1])
        row.deleted_at = now()
    assert not (await inbox(client, staff, reason="failed"))["items"]


async def test_review_failure_is_for_current_document_and_latest_attempt(auth_api):
    client, _, database = auth_api
    tokens = await login(client)
    customer, staff = headers(tokens), headers(await login(client, "admin"))
    p = await project(client, customer)
    document = await draft(client, customer, p["id"])
    async with database.session() as session:
        row = DocumentReview(
            project_id=UUID(p["id"]),
            item_id=UUID(document["id"]),
            actor_id=UUID(tokens["user"]["id"]),
            request_id=uuid4(),
            fingerprint="test",
            status="failed",
            source={"version": 1},
            blocks=[],
            report={},
            candidate="",
            events=[],
            error="private failure",
            created_at=now(),
        )
        session.add(row)
        await session.flush()
        rid = row.id
    result = await inbox(client, staff, reason="failed")
    assert result["items"][0]["failure_summary"] == "文档检查未完成"
    # Success on a different document must not hide this document's unresolved failure.
    async with database.session() as session:
        other = ProjectItem(
            project_id=UUID(p["id"]),
            kind="change",
            title="其他变更",
            content="无关正文",
            status="draft",
            created_by=UUID(tokens["user"]["id"]),
            created_at=now(),
            version=1,
        )
        session.add(other)
        await session.flush()
        session.add(
            DocumentReview(
                project_id=UUID(p["id"]),
                item_id=other.id,
                actor_id=UUID(tokens["user"]["id"]),
                request_id=uuid4(),
                fingerprint="other",
                status="ready",
                source={"version": 1},
                created_at=now() + timedelta(seconds=2),
            )
        )
    assert (await inbox(client, staff, reason="failed"))["items"][0][
        "failure_summary"
    ] == "文档检查未完成"
    async with database.session() as session:
        (await session.get(ProjectItem, UUID(document["id"]))).version += 1
    assert not (await inbox(client, staff, reason="failed"))["items"]
    async with database.session() as session:
        (await session.get(DocumentReview, rid)).source = {"version": 2}
        (await session.get(DocumentReview, rid)).status = "ready"
    assert not (await inbox(client, staff, reason="failed"))["items"]


async def test_inbox_permissions_and_query_bounds(auth_api):
    client, _, _ = auth_api
    customer, staff = headers(await login(client)), headers(await login(client, "admin"))
    assert (await client.get("/api/conversations/requests", headers=customer)).status_code == 403
    for params in ({"reason": "made-up"}, {"limit": 101}, {"offset": -1}, {"scope": "user"}):
        assert (
            await client.get("/api/conversations/requests", headers=staff, params=params)
        ).status_code == 422


@pytest.mark.skipif(os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="local PG opt-in")
async def test_real_pg_inbox_scope_and_effective_deadline(postgres_auth):
    service = postgres_auth
    for name in ("first_admin", "second_admin"):
        await service.create_user(name, name, "admin", "Postgres-test-password")
    actors = []
    for name in ("alice", "first_admin", "second_admin"):
        bundle = await service.login(name, "Postgres-test-password")
        actors.append(await service.authenticate(bundle.access_token))
    customer, first, second = actors
    store = ConversationStore(service.store.database)
    p = await store.start_consultation(customer, StartConsultation(request_id=uuid4()))
    async with service.store.database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.conversation_mode = "human"
        row.support_id = first.account.id
        row.support_activity_at = now() - timedelta(seconds=870)
        row.support_wait_since = now()
    assert not (await store.requests(second))["items"]
    result = await store.requests(second, scope="all", reason="expiring")
    assert len(result["items"]) == 1 and result["items"][0]["support_name"] == "first_admin"
    assert "expiring" in result["items"][0]["reasons"]
    async with service.store.database.session() as session:
        row = await session.get(Project, UUID(p["id"]))
        row.support_extended_until = now() + timedelta(minutes=6)
    assert not (await store.requests(second, scope="all", reason="expiring"))["items"]

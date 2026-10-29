# ruff: noqa: F811
import asyncio
import importlib
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import act, baseline, draft, project

from backend.db.project_store import ProjectStore, now
from backend.models.projects import (
    ProjectError,
    ProjectItem,
    SendMessage,
    StartConsultation,
)
from backend.models.review import DocumentReview, DocumentRevision


async def exercise_regeneration(store, principal, pid, *, concurrent=False):
    before = await store.detail(principal, pid)
    original = before["items"][0]
    run, _ = await store.start_run(
        principal, pid, SendMessage(request_id=uuid4(), content="重新生成")
    )
    rid = UUID(run["id"])
    body = {
        "kind": "specification",
        "title": "最新需求",
        "content": "新的详细开发方案",
        "questions": [],
    }

    async def finish():
        return await store.finish(
            principal,
            pid,
            rid,
            0,
            "已更新最新需求",
            [body],
            expected_version=before["project"]["version"],
            consultation={"last_run_id": str(rid)},
        )

    if concurrent:
        versions = await asyncio.gather(finish(), finish())
        assert versions[0] == versions[1]
    else:
        await finish()
        await finish()
    after = await store.detail(principal, pid)
    assert len(after["items"]) == 1
    current = after["items"][0]
    assert current["id"] == original["id"] and current["content"] == body["content"]
    assert current["status"] == "draft" and current["version"] == original["version"] + 1
    assert not current["customer_confirmed"] and not current["staff_confirmed"]
    assert after["project"]["revision"] == 0
    async with store.database.session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DocumentRevision)
                .where(DocumentRevision.item_id == UUID(current["id"]))
            )
            == 0
        )
    return current


async def test_regeneration_updates_one_record_and_old_confirmation_cannot_publish(auth_api):
    client, auth, database = auth_api
    token = await login(client)
    customer, admin = headers(token), headers(await login(client, "admin"))
    p = await project(client, customer)
    row = await draft(client, customer, p["id"])
    row = await act(client, customer, row, "submit")
    row = await act(client, customer, row, "confirm")
    principal = await auth.authenticate(token["access_token"])
    current = await exercise_regeneration(ProjectStore(database), principal, UUID(p["id"]))
    assert (await client.get(f"/api/projects/{p['id']}", headers=admin)).json()["items"][0][
        "id"
    ] == current["id"]
    stale = await client.post(
        f"/api/projects/{p['id']}/items/{row['id']}/actions",
        headers=admin,
        json={"request_id": str(uuid4()), "expected_version": row["version"], "action": "confirm"},
    )
    assert stale.status_code == 409
    duplicate = await client.post(
        f"/api/projects/{p['id']}/items",
        headers=customer,
        json={
            "request_id": str(uuid4()),
            "kind": "specification",
            "title": "另一份",
            "content": "重复初始需求",
            "base_revision": 0,
        },
    )
    assert (
        duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "REQUIREMENT_EXISTS"
    )


async def test_published_baseline_is_not_overwritten_by_regeneration(auth_api):
    client, auth, database = auth_api
    token = await login(client)
    customer, admin = headers(token), headers(await login(client, "admin"))
    p = await project(client, customer)
    old = await baseline(client, customer, admin, p["id"])
    principal = await auth.authenticate(token["access_token"])
    store = ProjectStore(database)
    run, _ = await store.start_run(
        principal, UUID(p["id"]), SendMessage(request_id=uuid4(), content="重新生成")
    )
    with pytest.raises(ProjectError) as failure:
        await store.finish(
            principal,
            UUID(p["id"]),
            UUID(run["id"]),
            1,
            "未发布",
            [
                {
                    "kind": "specification",
                    "title": "不能覆盖",
                    "content": "不能覆盖正式版",
                    "questions": [],
                }
            ],
        )
    assert failure.value.code == "BASELINE_EXISTS"
    current = await store.detail(principal, UUID(p["id"]))
    assert current["baseline"]["content"] == old["content"] and len(current["items"]) == 1


@pytest.mark.parametrize("status", ["withdraw", "reject"])
async def test_current_requirement_can_be_edited_after_withdrawal_or_rejection(auth_api, status):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    row = await draft(client, customer, p["id"])
    row = await act(client, admin if status == "reject" else customer, row, status, note="需要修改")
    edited = await act(
        client,
        customer,
        row,
        "edit",
        title="修订后的最新需求",
        content="修订后的正文",
        questions=[],
    )
    assert edited["id"] == row["id"] and edited["status"] == "draft" and not edited["resolution"]
    assert (
        len((await client.get(f"/api/projects/{p['id']}", headers=customer)).json()["items"]) == 1
    )


async def test_failed_regeneration_preserves_the_existing_complete_document(auth_api, monkeypatch):
    from drafting_support import DraftModel
    from test_consultation import install_model, send

    client, _, _ = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)
    customer = headers(await login(client))
    p = await project(client, customer)
    old = await draft(client, customer, p["id"], content="已经保存的完整详细开发说明")
    await send(client, customer, p["id"], "记录预约流程")
    model.fail_write = True
    await send(client, customer, p["id"], "重新生成需求草案")
    detail = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert len(detail["items"]) == 1 and detail["items"][0]["id"] == old["id"]
    assert detail["items"][0]["content"] == old["content"]


@pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires PostgreSQL"
)
async def test_real_pg_regeneration_receipt_is_atomic(postgres_auth):
    from backend.models.projects import CreateItem

    principal = await postgres_auth.authenticate(
        (await postgres_auth.login("alice", "Postgres-test-password")).access_token
    )
    store = ProjectStore(postgres_auth.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    await store.create_item(
        principal,
        UUID(p["id"]),
        CreateItem(
            request_id=uuid4(),
            kind="specification",
            title="初稿",
            content="旧内容",
            base_revision=0,
        ),
    )
    await exercise_regeneration(store, principal, UUID(p["id"]), concurrent=True)


@pytest.mark.skipif(
    os.environ.get("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Requires PostgreSQL"
)
async def test_real_pg_migration_removes_duplicate_bodies_and_keeps_latest(postgres_auth):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from backend.models.projects import CreateItem

    principal = await postgres_auth.authenticate(
        (await postgres_auth.login("alice", "Postgres-test-password")).access_token
    )
    database = postgres_auth.store.database
    store = ProjectStore(database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    create = CreateItem(
        request_id=uuid4(), kind="specification", title="旧草案", content="旧正文", base_revision=0
    )
    old = await store.create_item(principal, UUID(p["id"]), create)
    async with database.engine.begin() as connection:

        def drop_index(sync):
            schema = sync.get_execution_options()["schema_translate_map"][None]
            sync.execute(text(f'DROP INDEX "{schema}".uq_current_requirement'))

        await connection.run_sync(drop_index)
    newest = uuid4()
    async with database.session() as session:
        session.add(
            ProjectItem(
                id=newest,
                project_id=UUID(p["id"]),
                created_by=principal.account.id,
                created_at=now() + timedelta(seconds=1),
                kind="specification",
                title="最新草案",
                content="最新正文",
            )
        )
        session.add(
            DocumentRevision(
                project_id=UUID(p["id"]),
                item_id=UUID(old["id"]),
                actor_id=principal.account.id,
                version=1,
                title="旧历史",
                content="旧历史正文",
                questions=[],
                blocks=[],
                action="before_edit",
                created_at=now(),
            )
        )
        session.add(
            DocumentReview(
                project_id=UUID(p["id"]),
                item_id=UUID(old["id"]),
                actor_id=principal.account.id,
                request_id=uuid4(),
                fingerprint="x" * 64,
                status="ready",
                source={"version": 1, "content": "旧正文"},
                created_at=now(),
            )
        )
    async with database.engine.begin() as connection:

        def upgrade(sync):
            schema = sync.get_execution_options()["schema_translate_map"][None]
            sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            with Operations.context(MigrationContext.configure(sync)):
                importlib.import_module(
                    "backend.db.migrations.versions.0018_current_requirement"
                ).upgrade()

        await connection.run_sync(upgrade)
    detail = await store.detail(principal, UUID(p["id"]))
    assert [i["id"] for i in detail["items"]] == [str(newest)]
    with pytest.raises(ProjectError) as failure:
        await store.create_item(principal, UUID(p["id"]), create)
    assert failure.value.code == "REQUIREMENT_SUPERSEDED"
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentReview)) == 0
        assert await session.scalar(select(func.count()).select_from(DocumentRevision)) == 0
    with pytest.raises(IntegrityError):
        async with database.session() as session:
            session.add(
                ProjectItem(
                    project_id=UUID(p["id"]),
                    kind="specification",
                    title="非法重复",
                    content="重复",
                    created_by=principal.account.id,
                    created_at=now(),
                )
            )
            await session.flush()

"""P2 atomic related-block updates, exact inverse patches and scoped retry/undo."""

# ruff: noqa: F811
import asyncio
import copy
import importlib
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, text
from test_auth import auth_api, headers, login  # noqa: F401
from test_consultation import extracted, install_model, send
from test_draft_sections import AgentFlow, call, tools
from test_handoff import root_task
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import act, draft, project

from backend.agents.drafting.graph import create_draft
from backend.db.project_store import ProjectStore, now
from backend.models.documents import (
    DocumentUndo,
    apply_inverse,
    bounded_revision,
    build_document_index,
    content_hash,
    revision_edits,
    validate_revision_markdown,
)
from backend.models.projects import (
    CreateItem,
    DraftItem,
    Project,
    ProjectError,
    ProjectItem,
    ProjectRun,
    StartConsultation,
    UndoDocument,
)
from backend.models.review import DocumentRevision

SOURCE = (
    "  # 会议室需求\r\n背景保留，包括 Ca²⁺ 和 😀。\r\n\r\n"
    "## 预约规则\r\n每次最多2小时。\r\n\r\n"
    "## 数据设计\r\n```sql\r\nCHECK (hours <= 2);\r\n```\r\n\r\n"
    "## 验收\r\n预约2小时成功，超过2小时拒绝。\r\n\r\n"
    "## 客户原话\r\n我只需要一个简单系统。\r\n  "
)


def revision_flow(*, fail=None, verified=True, noop=False, source=SOURCE):
    flow = AgentFlow()
    flow.task = root_task()
    flow.principal = SimpleNamespace(
        account=SimpleNamespace(id=flow.task.actor_id), session_id=flow.task.session_id
    )
    flow.project_id, flow.run_id = flow.task.project_id, flow.task.run_id
    flow.snapshot["project"]["version"] = flow.task.project_version
    flow.prompt, flow.document_action = "把最长预约时间更新为3小时，数据和验收同步", "revise"
    index = build_document_index(uuid4(), 7, source)
    blocks = index.read_blocks(source)
    targets = [b["id"] for b in blocks if b["title"] in {"预约规则", "数据设计", "验收"}]
    flow.script = [
        tools(
            call(
                "plan_revision",
                {
                    "targets": [] if noop else targets,
                    "related": targets if noop else [],
                    "rationale": "时限变动需同步规则、数据校验和验收",
                },
            ),
            call("read_revision_blocks", {"ids": targets}),
            *([] if noop else [call("write_prd_section", {"section_id": sid}) for sid in targets]),
            call("finish_draft", {}),
        )
    ]
    flow.original = {
        "id": str(index.item_id),
        "version": 7,
        "title": "会议室需求",
        "questions": [],
        "content": source,
        "blocks": blocks,
    }

    async def target(*args):
        return copy.deepcopy(flow.original)

    async def model(system, data, **kwargs):
        flow.models.append((system, data, kwargs))
        flow.rounds += 1
        if "REVISION_SECTION" in system:
            sid = data["current_section"]["id"]
            flow.section_writes.append(sid)
            if sid == fail or fail == "all":
                raise TimeoutError("provider private detail")
            return {"content": data["original_section"].replace("2", "3")}
        if "REVISION_VERIFY" in system:
            return {"content": json.dumps({"passed": verified, "explanation": "核对三个关联块"})}
        return flow.script.pop(0) if flow.script else {"content": ""}

    flow.store.draft_target, flow.model = target, model
    return flow


async def test_five_node_graph_updates_related_blocks_and_preserves_raw_other_text():
    flow = revision_flow()
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["document"]["content"] == SOURCE.replace("2", "3")
    assert result["replacement"]["incremental"] and not result["no_change"]
    assert len(flow.section_writes) == 3 and flow.rounds == 5
    assert result["handoff"]["outcome"] == "completed"
    assert flow.original["content"] == SOURCE
    assert all(
        "原话" not in data["current_section"]["title"]
        for system, data, _ in flow.models
        if "REVISION_SECTION" in system
    )
    assert any(e.get("node") == "verify" for e in flow.events)


@pytest.mark.parametrize("options", [{"fail": "all"}, {"verified": False}])
async def test_related_failure_rejects_entire_candidate_without_degrading_old_body(options):
    flow = revision_flow(**options)
    with pytest.raises(ProjectError, match="原稿保持不变"):
        await create_draft(flow, checkpointer=InMemorySaver())
    assert flow.original["content"] == SOURCE and "private" not in str(flow.events)


async def test_noop_requires_read_evidence_and_verification():
    flow = revision_flow(noop=True)
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["no_change"] and result["document"]["content"] == SOURCE
    assert result["handoff"]["outcome"] == "no_change" and flow.section_writes == []


@pytest.mark.parametrize("fault", ["unread", "protected", "outside", "unclosed"])
async def test_invalid_scope_cannot_be_committed_by_graph(fault):
    flow = revision_flow()
    args = flow.script[0]["tool_calls"]
    if fault == "unread":
        del args[1]
    elif fault == "protected":
        flow.prompt += "，不要改预约规则"
    elif fault == "outside":
        args[2] = call("write_prd_section", {"section_id": flow.original["blocks"][0]["id"]})
    else:
        real = flow.model

        async def model(system, data, **kwargs):
            response = await real(system, data, **kwargs)
            return (
                {"content": "## 数据设计\n```sql\nSELECT 1;"}
                if "REVISION_SECTION" in system
                else response
            )

        flow.model = model
    with pytest.raises(ProjectError):
        await create_draft(flow, checkpointer=InMemorySaver())
    assert flow.original["content"] == SOURCE


def test_minimal_patch_roundtrip_offsets_cover_insert_delete_unicode_and_newlines():
    index = build_document_index(uuid4(), 1, SOURCE)
    blocks = index.read_blocks(SOURCE)
    replacements = {
        blocks[1]["id"]: blocks[1]["content"].replace("2", "300"),
        blocks[2]["id"]: blocks[2]["content"].replace("CHECK (hours <= 2);", ""),
        blocks[3]["id"]: "## 验收\r\n😀 新验收。\r\n\r\n",
    }
    content = "".join(replacements.get(b["id"], b["content"]) for b in blocks)
    edits = revision_edits(blocks, replacements)
    assert apply_inverse(content, edits) == SOURCE
    assert sum(len(e["old_text"]) for e in edits) < len(SOURCE) // 2
    with pytest.raises(ValueError):
        apply_inverse("wrong" + content, edits)
    with pytest.raises(ValueError):
        bounded_revision([{"old_text": "x" * 1000, "new_text": "y"}], "x" * 1000)
    with pytest.raises(ValueError):
        bounded_revision([], SOURCE, {"questions": "x" * 65536})


def test_fence_validation_uses_parsed_closure_including_nested_containers():
    for source in (
        "```sql\nSELECT 1;\n```\n",
        "> ```sql\n> SELECT 1;\n> ```\n",
        "- ```\n  line\n  ```\n",
        "~~~\n~~~",
    ):
        validate_revision_markdown(source)
    for source in ("```sql\nSELECT 1;\n", "```sql\nSELECT 1;\n    ```", "> ```\n> line\n"):
        with pytest.raises(ValueError):
            validate_revision_markdown(source)


async def setup_store(client, service, database, source=SOURCE):
    tokens = await login(client)
    principal = await service.authenticate(tokens["access_token"])
    identity = headers(tokens)
    p = await project(client, identity)
    item = await draft(client, identity, p["id"], content=source)
    return ProjectStore(database), principal, identity, item


async def commit(store, principal, item, *, replacements=None, fault=None):
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    index = await store.document_index(principal, pid, iid)
    blocks = build_document_index(iid, item["version"], item["content"]).read_blocks(
        item["content"]
    )
    # Use the authoritative persisted IDs, including explicitly mapped duplicate headings.
    for block, meta in zip(blocks, index["blocks"], strict=True):
        block["id"] = meta["id"]
    if replacements is None:
        replacements = {blocks[1]["id"]: blocks[1]["content"].replace("2", "3")}
    candidate = "".join(replacements.get(b["id"], b["content"]) for b in blocks)
    payload = {
        "id": str(iid),
        "version": item["version"],
        "title": item["title"],
        "hash": content_hash(item["content"]),
        "incremental": True,
        "replacements": replacements,
        "target_ids": list(replacements),
        "read_ids": list(replacements),
        "related_ids": [],
        "protected_ids": [],
    }
    if fault:
        payload.update(fault)
    async with store.database.session() as session:
        run = ProjectRun(
            project_id=pid,
            actor_id=principal.account.id,
            request_id=uuid4(),
            prompt="将要求更新到草案",
            status="completed",
            created_at=now(),
        )
        session.add(run)
        await session.flush()
        rid = run.id
    async with store._write_session() as session:
        await store._project(session, principal, pid, write=True)
        row = await session.get(ProjectItem, iid)
        run = await session.get(ProjectRun, rid)
        changed = await store._commit_incremental(
            session,
            row,
            DraftItem(
                kind="specification",
                title=item["title"],
                content=candidate,
                questions=item["questions"],
            ),
            payload,
            run,
        )
    return await store.item(principal, pid, iid), changed


async def test_store_atomic_diff_undo_idempotency_and_customer_facts_survive(auth_api):
    client, service, database = auth_api
    store, principal, identity, item = await setup_store(client, service, database)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    before = await store.document_index(principal, pid, iid)
    async with database.session() as session:
        p = await session.get(Project, pid)
        p.discovery = {"scope": ["最长3小时"]}
    updated, changed = await commit(store, principal, item)
    assert changed and updated["version"] == item["version"] + 1
    latest = await store.latest_document_change(principal, pid, iid)
    assert latest["can_undo"] and latest["change"]["edits"][0]["old_text"] == "2"
    command = {
        "request_id": str(uuid4()),
        "change_id": latest["change"]["id"],
        "expected_version": updated["version"],
    }
    url = f"/api/projects/{pid}/items/{iid}/undo-change"
    bob = headers(await login(client, "bob"))
    assert (await client.post(url, headers=bob, json=command)).status_code == 404
    stale = await client.post(url, headers=identity, json={**command, "expected_version": 1})
    assert stale.status_code == 409
    response = await client.post(url, headers=identity, json=command)
    assert response.status_code == 200, response.text
    restored = response.json()["item"]
    assert restored["content"] == SOURCE and restored["version"] == updated["version"] + 1
    assert not restored["customer_confirmed"] and not restored["staff_confirmed"]
    assert (await client.post(url, headers=identity, json=command)).json() == response.json()
    assert (
        await client.get(url.replace("undo-change", "latest-change"), headers=bob)
    ).status_code == 404
    assert (await store.latest_document_change(principal, pid, iid))["change"] is None
    after = await store.document_index(principal, pid, iid)
    assert [b["id"] for b in before["blocks"]] == [b["id"] for b in after["blocks"]]
    async with database.session() as session:
        p = await session.get(Project, pid)
        assert p.discovery == {"scope": ["最长3小时"]} and p.consultation["document_pending"]
        assert await session.scalar(select(func.count()).select_from(DocumentRevision)) == 0


@pytest.mark.parametrize("fault", [{"hash": "0" * 64}, {"read_ids": []}, {"target_ids": []}])
async def test_store_rechecks_manifest_scope_and_hash_even_for_internal_candidate(auth_api, fault):
    client, service, database = auth_api
    store, principal, _, item = await setup_store(client, service, database)
    with pytest.raises(ProjectError):
        await commit(store, principal, item, fault=fault)
    assert (await store.item(principal, UUID(item["project_id"]), UUID(item["id"])))[
        "content"
    ] == SOURCE
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentUndo)) == 0


async def test_only_last_patch_kept_noop_preserves_signatures_and_manual_edit_invalidates(auth_api):
    client, service, database = auth_api
    store, principal, identity, item = await setup_store(client, service, database)
    first, _ = await commit(store, principal, item)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    original_change = await store.latest_document_change(principal, pid, iid)
    current_index = await store.document_index(principal, pid, iid)
    sid = current_index["blocks"][1]["id"]
    block = first["content"][
        current_index["blocks"][1]["start"] : current_index["blocks"][1]["end"]
    ]
    second, _ = await commit(store, principal, first, replacements={sid: block.replace("3", "4")})
    latest = await store.latest_document_change(principal, pid, iid)
    assert latest["change"]["id"] != original_change["change"]["id"]
    async with database.session() as session:
        row = await session.get(ProjectItem, iid)
        row.status, row.customer_confirmed = "submitted", True
        assert await session.scalar(select(func.count()).select_from(DocumentUndo)) == 1
    unchanged, changed = await commit(store, principal, second, replacements={})
    assert (
        not changed
        and unchanged["version"] == second["version"]
        and unchanged["customer_confirmed"]
    )
    assert (await store.latest_document_change(principal, pid, iid))["change"] == latest["change"]
    edited = await act(
        client,
        identity,
        unchanged,
        "edit",
        title=item["title"],
        content=unchanged["content"] + "\n人工修改",
    )
    assert edited["version"] > unchanged["version"]
    assert (await store.latest_document_change(principal, pid, iid))["change"] is None


async def test_duplicate_heading_identity_survives_explicit_update_and_exact_undo(auth_api):
    client, service, database = auth_api
    source = "## 同名\nold\n\n## 同名\nkeep\n\n## 其他\nother\n"
    store, principal, _, item = await setup_store(client, service, database, source)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    before = await store.document_index(principal, pid, iid)
    updated, _ = await commit(
        store, principal, item, replacements={before["blocks"][0]["id"]: "## 同名\nnew\n\n"}
    )
    after = await store.document_index(principal, pid, iid)
    assert [b["id"] for b in before["blocks"]] == [b["id"] for b in after["blocks"]]
    latest = await store.latest_document_change(principal, pid, iid)
    result = await store.undo_document_change(
        principal,
        pid,
        iid,
        UndoDocument(
            request_id=uuid4(),
            expected_version=updated["version"],
            change_id=latest["change"]["id"],
        ),
    )
    assert result["item"]["content"] == source
    restored = await store.document_index(principal, pid, iid)
    assert [b["id"] for b in restored["blocks"]] == [b["id"] for b in before["blocks"]]


async def test_published_baseline_invalidates_undo_and_remains_immutable(auth_api):
    client, service, database = auth_api
    store, principal, identity, item = await setup_store(client, service, database)
    updated, _ = await commit(store, principal, item)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    latest = await store.latest_document_change(principal, pid, iid)
    updated = await act(client, identity, updated, "submit")
    updated = await act(client, identity, updated, "confirm")
    assert (await store.latest_document_change(principal, pid, iid))["can_undo"]
    published = await act(client, headers(await login(client, "admin")), updated, "confirm")
    assert published["published_revision"] == 1
    assert (await store.latest_document_change(principal, pid, iid))["change"] is None
    with pytest.raises(ProjectError):
        await store.undo_document_change(
            principal,
            pid,
            iid,
            UndoDocument(
                request_id=uuid4(),
                expected_version=published["version"],
                change_id=latest["change"]["id"],
            ),
        )
    assert (await store.item(principal, pid, iid))["content"] == published["content"]


async def test_running_work_rejects_undo_without_consuming_latest_diff(auth_api):
    client, service, database = auth_api
    store, principal, _, item = await setup_store(client, service, database)
    updated, _ = await commit(store, principal, item)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    latest = await store.latest_document_change(principal, pid, iid)
    async with database.session() as session:
        session.add(
            ProjectRun(
                project_id=pid,
                actor_id=principal.account.id,
                request_id=uuid4(),
                prompt="busy",
                status="running",
                created_at=now(),
            )
        )
    with pytest.raises(ProjectError) as error:
        await store.undo_document_change(
            principal,
            pid,
            iid,
            UndoDocument(
                request_id=uuid4(),
                expected_version=updated["version"],
                change_id=latest["change"]["id"],
            ),
        )
    assert error.value.code == "PROJECT_BUSY"
    assert (await store.item(principal, pid, iid))["content"] == updated["content"]
    assert (await store.latest_document_change(principal, pid, iid))["change"] == latest["change"]


async def test_unclosed_fence_and_protected_customer_appendix_are_atomic_store_rejections(auth_api):
    client, service, database = auth_api
    store, principal, _, item = await setup_store(client, service, database)
    pid, iid = UUID(item["project_id"]), UUID(item["id"])
    index = await store.document_index(principal, pid, iid)
    for replacements in (
        {index["blocks"][1]["id"]: "## 预约规则\n```sql\nSELECT 1;"},
        {index["blocks"][-1]["id"]: "## 客户原话\n篡改客户原话\n"},
        {index["blocks"][1]["id"]: "## 预约规则\nno newline"},
    ):
        with pytest.raises(ProjectError):
            await commit(store, principal, item, replacements=replacements)
        assert (await store.item(principal, pid, iid))["content"] == SOURCE


@pytest.mark.parametrize("fail_related", [False, True])
async def test_real_queue_graph_store_handoff_saves_facts_before_atomic_document_update(
    auth_api, monkeypatch, fail_related
):
    client, service, database = auth_api
    store, principal, identity, item = await setup_store(client, service, database)
    prompt = "最长3小时，把这条要求更新到需求草案，数据校验和验收同步修改"

    class Model:
        async def respond(self, messages):
            task = messages[0]["content"]
            data = json.loads(messages[1]["content"].split("以下为业务数据：", 1)[1])
            if "CONSULTATION_ADVISE" in task:
                return extracted(prompt, {"scope": ["最长3小时"]}, draft_request_quote=prompt)
            if "REVISION_SECTION" in task:
                if fail_related and "数据设计" in data["current_section"]["title"]:
                    raise TimeoutError("private failure")
                return {"content": data["original_section"].replace("2", "3")}, {
                    "total_tokens": 100
                }
            if "REVISION_VERIFY" in task:
                return {"content": '{"passed":true,"explanation":"规则、数据和验收一致"}'}, {
                    "total_tokens": 100
                }
            if "局部修订Agent" in task:
                targets = [
                    b["id"]
                    for b in data["existing_sections"]
                    if b["title"] in {"预约规则", "数据设计", "验收"}
                ]
                return tools(
                    call("plan_revision", {"targets": targets, "rationale": "同步时限"}),
                    call("read_revision_blocks", {"ids": targets}),
                    *[call("write_prd_section", {"section_id": sid}) for sid in targets],
                    call("finish_draft", {}),
                ), {"total_tokens": 100}
            raise AssertionError("unexpected model task")

    install_model(monkeypatch, Model())
    result = await send(client, identity, item["project_id"], prompt)
    assert result["status"] == "completed", result
    current = await store.item(principal, UUID(item["project_id"]), UUID(item["id"]))
    async with database.session() as session:
        saved_project = await session.get(Project, UUID(item["project_id"]))
        assert "最长3小时" in saved_project.discovery["scope"]
        assert saved_project.consultation["document_pending"] == fail_related
    if fail_related:
        assert current["content"] == SOURCE and current["version"] == item["version"]
        assert "保留不变" in result["answer"]
    else:
        assert current["content"] == SOURCE.replace("2", "3")
        assert "修改差异" in result["answer"]
        assert (
            await store.latest_document_change(
                principal, UUID(item["project_id"]), UUID(item["id"])
            )
        )["can_undo"]


@pytest.mark.skipif(os.getenv("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Enable PostgreSQL")
async def test_real_postgres_undo_serializes_and_migration_preserves_customer_body(postgres_auth):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    service = postgres_auth
    principal = await service.authenticate(
        (await service.login("alice", "Postgres-test-password")).access_token
    )
    store = ProjectStore(service.store.database)
    p = await store.start_consultation(principal, StartConsultation(request_id=uuid4()))
    pid = UUID(p["id"])
    item = await store.create_item(
        principal,
        pid,
        CreateItem(
            request_id=uuid4(),
            base_revision=0,
            kind="specification",
            title="P2临时验收",
            content=SOURCE,
        ),
    )
    iid = UUID(item["id"])
    async with store.database.engine.begin() as connection:

        def migrate(sync):
            schema = sync.get_execution_options()["schema_translate_map"][None]
            sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            step = importlib.import_module("backend.db.migrations.versions.0024_document_undo")
            with Operations.context(MigrationContext.configure(sync)):
                step.downgrade()
                step.upgrade()

        await connection.run_sync(migrate)
    assert (await store.item(principal, pid, iid))["content"] == SOURCE
    updated, _ = await commit(store, principal, item)
    latest = await store.latest_document_change(principal, pid, iid)
    command = UndoDocument(
        request_id=uuid4(), expected_version=updated["version"], change_id=latest["change"]["id"]
    )
    results = await asyncio.gather(
        *[store.undo_document_change(principal, pid, iid, command) for _ in range(2)]
    )
    assert results[0] == results[1] and results[0]["item"]["content"] == SOURCE
    assert results[0]["item"]["version"] == updated["version"] + 1

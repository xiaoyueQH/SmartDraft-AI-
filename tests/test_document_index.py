"""Stable source identities, read-only indexing, transaction and access boundaries."""

# ruff: noqa: F811
import asyncio
import copy
import importlib
import os
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import act, draft, project

from backend.db.project_store import ProjectStore
from backend.models.documents import (
    DocumentIndex,
    DocumentMetadata,
    build_document_index,
    content_hash,
)
from backend.models.projects import (
    CreateItem,
    Project,
    ProjectError,
    ProjectItem,
    StartConsultation,
)
from backend.models.review import DocumentRevision

SOURCE = (
    "# 需求书\r\n背景保持原文。\r\n\r\n"
    "## 预约规则\r\n只能操作本人预约，每次最多2小时。\r\n\r\n"
    "## 数据设计\r\n```sql\r\n## 不是章节\r\nSELECT 1;\r\n```\r\n\r\n"
    "## 验收\r\n取消后其他客户可再次预约。\r\n"
)


def test_insert_rename_and_move_preserve_identity_and_original_source_slices():
    iid = uuid4()
    original = build_document_index(iid, 1, SOURCE)
    chunks = original.read_blocks(SOURCE)
    moved = chunks[0]["content"] + "## 新章节\r\n新增内容。\r\n\r\n"
    moved += chunks[3]["content"] + chunks[1]["content"]
    moved += chunks[2]["content"].replace("## 数据设计", "## 数据库设计")
    updated = build_document_index(iid, 2, moved, original)
    before = {b.title: b.id for b in original.blocks}
    after = {b.title: b.id for b in updated.blocks}
    assert after["预约规则"] == before["预约规则"]
    assert after["验收"] == before["验收"]
    assert after["数据库设计"] == before["数据设计"]
    assert "".join(b["content"] for b in updated.read_blocks(moved)) == moved
    assert not updated.invalidated_ids


def test_ambiguous_duplicates_and_unrelated_last_pair_are_not_guessed():
    iid = uuid4()
    before = build_document_index(iid, 1, "## 同名\n同样内容\n\n## 同名\n同样内容\n\n")
    unchanged = build_document_index(iid, 2, "## 同名\n同样内容\n\n## 同名\n同样内容\n\n", before)
    assert [b.id for b in unchanged.blocks] == [b.id for b in before.blocks]
    after = build_document_index(
        iid, 3, "## 插入\n独立内容\n\n## 同名\n同样内容\n\n## 同名\n同样内容\n\n", before
    )
    assert not {b.id for b in before.blocks} & {b.id for b in after.blocks}
    assert set(after.invalidated_ids) == {b.id for b in before.blocks}
    assert after.mapping_status == "invalidated"
    single = build_document_index(iid, 3, "## 原业务\n旧规则。\n")
    changed = build_document_index(iid, 4, "## 完全不同的章节\n新增规则。\n", single)
    assert changed.blocks[0].id != single.blocks[0].id


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_unicode_offsets_are_codepoints_and_syntax_blocks_are_indivisible(newline):
    source = newline.join(
        [
            "# 文档😀",
            "说明含\u2028Unicode分隔符，不是换行。",
            "",
            "## 内容",
            "| 字段 | 说明 |",
            "|---|---|",
            "| 宠物 | 🐶 |",
            "",
            "- 外层",
            "  1. 内层",
            "     > ## 不是章节",
            "",
            "```python",
            "## 代码内不是标题",
            "print('😀')",
            "```",
            "",
            "末章",
            "----",
            "原文保留。",
        ]
    )
    index = build_document_index(uuid4(), 1, source)
    blocks = index.read_blocks(source)
    assert [b["title"] for b in blocks] == ["文档😀", "内容", "末章"]
    assert "".join(b["content"] for b in blocks) == source
    assert all(b["content"] == source[b["start"] : b["end"]] for b in blocks)
    assert "print('😀')" in blocks[1]["content"] and "| 宠物 | 🐶 |" in blocks[1]["content"]


def test_oversized_atomic_code_and_capacity_do_not_truncate_customer_document():
    source = "## 代码\n```text\n" + "未截断的原文😀\n" * 2000 + "```\n\n## 后续\n保留。"
    index = build_document_index(uuid4(), 1, source)
    assert index.blocks[0].oversized
    assert "".join(b["content"] for b in index.read_blocks(source)) == source
    source = "".join(f"## 章{i}\n正文。\n" for i in range(513))
    large = build_document_index(uuid4(), 1, source)
    assert large.status == "too_large" and not large.blocks
    assert large.content_length == len(source) and large.content_hash == content_hash(source)
    with pytest.raises(ValueError, match="目录容量"):
        large.read_blocks(source)


def test_metadata_contains_no_section_body_and_rejects_wrong_scope_or_corrupt_ranges():
    iid = uuid4()
    original = build_document_index(iid, 2, SOURCE)
    assert all("content" not in row for row in original.model_dump()["blocks"])
    with pytest.raises(ValueError, match="其他文档"):
        build_document_index(uuid4(), 3, SOURCE, original)
    with pytest.raises(ValueError, match="未来版本"):
        build_document_index(iid, 1, SOURCE, original)
    with pytest.raises(ValueError, match="正文不匹配"):
        original.read_blocks(SOURCE + "changed")
    data = original.model_dump()
    data["blocks"][0]["end"] += 1
    with pytest.raises(ValidationError):
        DocumentIndex.model_validate(data)


async def test_lazy_index_and_manual_edit_are_scoped_and_do_not_mutate_business_state(auth_api):
    client, _, database = auth_api
    alice, bob = headers(await login(client)), headers(await login(client, "bob"))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    item = await act(client, alice, item, "submit")
    item = await act(client, alice, item, "confirm")
    iid, pid = UUID(item["id"]), UUID(p["id"])
    async with database.session() as session:
        metadata = await session.get(DocumentMetadata, iid)
        await session.delete(metadata)  # Simulate a pre-index historical item.
        before = (await session.get(Project, pid)).version
    url = f"/api/projects/{pid}/items/{iid}/blocks"
    assert (await client.get(url, headers=bob)).status_code == 404
    first = (
        await client.get(url, headers=alice, params={"expected_version": item["version"]})
    ).json()
    second = (await client.get(url, headers=alice)).json()
    assert first == second and first["content_hash"] == content_hash(item["content"])
    saved = (await client.get(f"/api/projects/{pid}/items/{iid}", headers=alice)).json()
    assert (saved["content"], saved["version"], saved["customer_confirmed"], saved["status"]) == (
        item["content"],
        item["version"],
        True,
        "submitted",
    )
    async with database.session() as session:
        assert (await session.get(Project, pid)).version == before
        assert await session.scalar(select(func.count()).select_from(DocumentMetadata)) == 1
    edited = await act(
        client,
        alice,
        item,
        "edit",
        title=item["title"],
        content=SOURCE.replace("## 数据设计", "## 数据库设计"),
    )
    assert (
        await client.get(url, headers=alice, params={"expected_version": item["version"]})
    ).status_code == 409
    index = (await client.get(url, headers=alice)).json()
    assert [b["id"] for b in first["blocks"]] == [b["id"] for b in index["blocks"]]
    assert index["content_version"] == edited["version"]
    async with database.session() as session:
        revision = await session.scalar(
            select(DocumentRevision).where(
                DocumentRevision.item_id == iid, DocumentRevision.version == item["version"]
            )
        )
        assert [b["id"] for b in revision.blocks] == [b["id"] for b in first["blocks"]]


@pytest.mark.parametrize("corruption", ["scope", "title", "parent", "capacity"])
async def test_corrupt_derived_index_is_rebuilt_without_touching_body(auth_api, corruption):
    client, _, database = auth_api
    alice = headers(await login(client))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    iid = UUID(item["id"])
    async with database.session() as session:
        metadata = await session.get(DocumentMetadata, iid)
        manifest = copy.deepcopy(metadata.manifest)
        if corruption == "scope":
            manifest = {**manifest, "item_id": str(uuid4())}
        elif corruption == "title":
            manifest["blocks"][1]["title"] = "伪造标题"
        elif corruption == "parent":
            manifest["blocks"][2]["parent_id"] = manifest["blocks"][1]["id"]
        else:
            manifest = {**manifest, "status": "too_large", "blocks": []}
        metadata.manifest = {**manifest}
    result = (await client.get(f"/api/projects/{p['id']}/items/{iid}/blocks", headers=alice)).json()
    assert result["mapping_status"] == "invalidated"
    async with database.session() as session:
        saved = await session.get(ProjectItem, iid)
        assert saved.content == item["content"] and saved.version == item["version"]


async def test_failed_edit_rolls_back_body_and_index_together(auth_api, monkeypatch):
    client, _, database = auth_api
    alice = headers(await login(client))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    iid = UUID(item["id"])
    async with database.session() as session:
        previous = (await session.get(DocumentMetadata, iid)).manifest
    original = ProjectStore._document_index

    async def fail(self, session, row):
        await original(self, session, row)
        raise ProjectError("SIMULATED", "受控失败", 409)

    monkeypatch.setattr(ProjectStore, "_document_index", fail)
    response = await client.post(
        f"/api/projects/{p['id']}/items/{iid}/actions",
        headers=alice,
        json={
            "request_id": str(uuid4()),
            "expected_version": item["version"],
            "action": "edit",
            "title": item["title"],
            "content": SOURCE.replace("2小时", "3小时"),
        },
    )
    assert response.status_code == 409
    async with database.session() as session:
        assert (await session.get(ProjectItem, iid)).content == item["content"]
        assert (await session.get(DocumentMetadata, iid)).manifest == previous


async def test_published_baseline_can_be_indexed_without_clearing_either_confirmation(auth_api):
    client, _, database = auth_api
    alice, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, alice)
    item = await draft(client, alice, p["id"], content=SOURCE)
    item = await act(client, alice, item, "submit")
    item = await act(client, alice, item, "confirm")
    item = await act(client, admin, item, "confirm")
    iid = UUID(item["id"])
    async with database.session() as session:
        await session.delete(await session.get(DocumentMetadata, iid))
    response = await client.get(f"/api/projects/{p['id']}/items/{iid}/blocks", headers=alice)
    assert response.status_code == 200
    async with database.session() as session:
        saved = await session.get(ProjectItem, iid)
        assert saved.customer_confirmed and saved.staff_confirmed and saved.published_revision == 1
        assert saved.status == "approved" and saved.version == item["version"]
        assert saved.content == item["content"]


@pytest.mark.skipif(os.getenv("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Enable PostgreSQL")
async def test_real_postgres_lazy_build_is_single_and_version_fenced(postgres_auth):
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
            title="目录隔离验证",
            content=SOURCE,
        ),
    )
    iid = UUID(item["id"])
    async with store.database.session() as session:
        await session.delete(await session.get(DocumentMetadata, iid))
    results = await asyncio.gather(
        *[store.document_index(principal, pid, iid, item["version"]) for _ in range(6)]
    )
    assert all(result == results[0] for result in results)
    async with store.database.session() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentMetadata)) == 1
        assert (await session.get(ProjectItem, iid)).content == item["content"]


@pytest.mark.skipif(os.getenv("SUPPORTFLOW_TEST_POSTGRES") != "1", reason="Enable PostgreSQL")
async def test_real_postgres_migration_is_derived_only_and_old_body_survives(postgres_auth):
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
            title="迁移前正文",
            content=SOURCE,
        ),
    )
    iid = UUID(item["id"])
    async with store.database.engine.begin() as connection:

        def migrate(sync):
            schema = sync.get_execution_options()["schema_translate_map"][None]
            sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            step = importlib.import_module("backend.db.migrations.versions.0023_document_metadata")
            with Operations.context(MigrationContext.configure(sync)):
                step.downgrade()
                step.upgrade()

        await connection.run_sync(migrate)
    async with store.database.session() as session:
        assert (await session.get(ProjectItem, iid)).content == item["content"]
        assert await session.get(DocumentMetadata, iid) is None
    index = await store.document_index(principal, pid, iid, item["version"])
    assert index["content_hash"] == content_hash(item["content"])

# ruff: noqa: F811
from uuid import UUID, uuid4

from sqlalchemy import func, select
from test_auth import auth_api, headers, login  # noqa: F401
from test_projects import act, baseline, draft, project

from backend.db.project_store import ProjectStore
from backend.models.projects import ProjectItem, ProjectRecordEvent, ProjectRun, SendMessage


async def current(client, identity, pid):
    response = await client.get(f"/api/projects/{pid}", headers=identity)
    assert response.status_code == 200, response.text
    return response.json()["project"]


async def manage(client, identity, records, action, **extra):
    return await client.post(
        "/api/projects/records/actions",
        headers=identity,
        json={
            "request_id": str(uuid4()),
            "action": action,
            "targets": [{"id": row["id"], "expected_version": row["version"]} for row in records],
            **extra,
        },
    )


async def test_rename_trash_restore_preserve_history_and_are_idempotent(auth_api):
    client, _, database = auth_api
    customer = headers(await login(client))
    p = await project(client, customer)
    document = await draft(client, customer, p["id"])
    p = await current(client, customer, p["id"])
    renamed = await manage(client, customer, [p], "rename", title="门店预约沟通")
    assert renamed.status_code == 200, renamed.text
    p = renamed.json()["items"][0]
    request_id = str(uuid4())
    deleted = await manage(client, customer, [p], "trash", request_id=request_id)
    repeated = await manage(client, customer, [p], "trash", request_id=request_id)
    assert deleted.status_code == 200 and deleted.json() == repeated.json()
    assert (await client.get(f"/api/projects/{p['id']}", headers=customer)).status_code == 404
    assert (
        await client.get(f"/api/projects/{p['id']}/items/{document['id']}", headers=customer)
    ).status_code == 404
    assert (await client.get("/api/projects", headers=customer)).json()["items"] == []
    trash = (await client.get("/api/projects?deleted=true", headers=customer)).json()["items"]
    assert len(trash) == 1 and trash[0]["title"] == "门店预约沟通"
    restored = await manage(client, customer, trash, "restore")
    assert restored.status_code == 200 and restored.json()["items"][0]["deleted_at"] is None
    assert (
        await client.get(f"/api/projects/{p['id']}/items/{document['id']}", headers=customer)
    ).json()["content"] == document["content"]
    async with database.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRecordEvent)) == 3
        assert await session.scalar(select(func.count()).select_from(ProjectItem)) == 1


async def test_batch_is_atomic_on_foreign_record_or_stale_version(auth_api):
    client, _, _ = auth_api
    alice, bob = headers(await login(client)), headers(await login(client, "bob"))
    first, second, foreign = (
        await project(client, alice),
        await project(client, alice),
        await project(client, bob),
    )
    response = await manage(client, alice, [first, foreign], "trash")
    assert response.status_code == 404
    assert (await current(client, alice, first["id"]))["deleted_at"] is None
    await manage(client, alice, [second], "rename", title="已更新名称")
    response = await manage(client, alice, [first, second], "trash")
    assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert (await current(client, alice, first["id"]))["deleted_at"] is None
    latest = await current(client, alice, second["id"])
    response = await manage(client, alice, [first, latest], "trash")
    assert response.status_code == 200
    assert len((await client.get("/api/projects?deleted=true", headers=alice)).json()["items"]) == 2
    assert (await client.get("/api/projects?deleted=true", headers=bob)).json()["items"] == []


async def test_manual_record_name_survives_agent_notes_and_project_confirmation(auth_api):
    client, auth, database = auth_api
    tokens = await login(client)
    customer, admin = headers(tokens), headers(await login(client, "admin"))
    principal = await auth.authenticate(tokens["access_token"])
    p = await project(client, customer)
    assert (await manage(client, customer, [p], "rename", title="我的门店项目")).status_code == 200
    store = ProjectStore(database)
    run, _ = await store.start_run(
        principal, UUID(p["id"]), SendMessage(request_id=uuid4(), content="补充业务情况")
    )
    await store.finish(
        principal,
        UUID(p["id"]),
        UUID(run["id"]),
        0,
        "已记录业务情况",
        [],
        [{"subject": "顾问拟定的名称", "business_background": "门店预约"}],
    )
    assert (await current(client, customer, p["id"]))["title"] == "我的门店项目"
    await baseline(client, customer, admin, p["id"])
    assert (await current(client, customer, p["id"]))["title"] == "我的门店项目"


async def test_formal_project_management_requires_admin_and_busy_run_blocks_delete(auth_api):
    client, _, database = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    await baseline(client, customer, admin, p["id"])
    p = await current(client, customer, p["id"])
    assert (await manage(client, customer, [p], "trash")).status_code == 403
    async with database.session() as session:
        from backend.db.project_store import now

        session.add(
            ProjectRun(
                id=uuid4(),
                project_id=UUID(p["id"]),
                actor_id=UUID(p["owner_id"]),
                request_id=uuid4(),
                prompt="正在整理",
                status="running",
                events=[],
                answer="",
                created_at=now(),
            )
        )
    response = await manage(client, admin, [p], "trash")
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROJECT_BUSY"


async def test_complete_business_lifecycle_with_changes_delivery_support_and_recovery(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    original = await baseline(client, customer, admin, p["id"])
    assert (await current(client, customer, p["id"]))["phase"] == "project"
    task = await draft(client, admin, p["id"], "task", 1, title="实现入库流程")
    for identity, action, note in (
        (admin, "submit", ""),
        (admin, "start", ""),
        (customer, "supplement", "入库数量必须是正整数"),
        (admin, "deliver", "提交实现说明和正整数校验结果"),
        (customer, "accept", "按清单检查通过"),
    ):
        task = await act(client, identity, task, action, note=note)
    assert task["status"] == "closed"
    delivery = await draft(client, admin, p["id"], "acceptance", 1, title="首版交付验收")
    for identity, action, note in (
        (admin, "submit", ""),
        (admin, "start", ""),
        (admin, "deliver", "已提供使用说明和验收清单"),
        (customer, "reopen", "缺少导出使用说明"),
        (admin, "deliver", "已补全使用说明"),
        (customer, "accept", "验收通过"),
    ):
        delivery = await act(client, identity, delivery, action, note=note)
    changed = await draft(
        client,
        customer,
        p["id"],
        "change",
        1,
        title="需求书第二版",
        content=original["content"] + "\n变更：新增库存预警，删除短信通知，改为站内提醒。",
    )
    comparison = await client.get(
        f"/api/projects/{p['id']}/items/{changed['id']}/comparison?expected_version={changed['version']}",
        headers=customer,
    )
    assert (
        comparison.json()["before"] == original["content"]
        and "库存预警" in comparison.json()["after"]
    )
    for identity, action in ((customer, "submit"), (customer, "confirm"), (admin, "confirm")):
        changed = await act(client, identity, changed, action)
    assert changed["published_revision"] == 2
    ticket = await draft(
        client,
        customer,
        p["id"],
        "bug",
        2,
        title="预警未触发",
        content="数量低于阈值，实际没有提醒，单商品可复现",
    )
    for identity, action, note in (
        (customer, "submit", ""),
        (admin, "start", ""),
        (customer, "supplement", "补充：修改阈值后出现"),
        (admin, "deliver", "提供修复说明和验证记录"),
        (customer, "reopen", "多商品仍需复核"),
        (admin, "deliver", "补充多商品验证结果"),
        (customer, "accept", "复测通过"),
    ):
        ticket = await act(client, identity, ticket, action, note=note)
    assert ticket["status"] == "closed"
    latest = await current(client, admin, p["id"])
    deleted = await manage(client, admin, [latest], "trash")
    assert deleted.status_code == 200
    restored = await manage(client, admin, deleted.json()["items"], "restore")
    assert restored.status_code == 200
    detail = (await client.get(f"/api/projects/{p['id']}", headers=customer)).json()
    assert detail["project"]["revision"] == 2 and detail["baseline"]["id"] == changed["id"]
    assert (
        await client.get(f"/api/projects/{p['id']}/items/{original['id']}", headers=customer)
    ).json()["content"] == original["content"]
    history = (await client.get(f"/api/projects/{p['id']}/activity", headers=customer)).json()
    assert any(event["note"] == "补充：修改阈值后出现" for event in history)
    assert any(event["action"] == "record:restore" for event in history)

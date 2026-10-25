# ruff: noqa: F811
from uuid import uuid4

from test_auth import auth_api, headers, login  # noqa: F401
from test_projects import act, baseline, draft, project


async def test_admin_workspace_counts_filters_and_role_boundaries(auth_api):
    client, _, _ = auth_api
    alice, bob, admin = (
        headers(await login(client)),
        headers(await login(client, "bob")),
        headers(await login(client, "admin")),
    )
    a = await project(client, alice)
    b = await project(client, bob)
    await baseline(client, alice, admin, a["id"])
    change = await draft(client, alice, a["id"], "change", revision=1, title="增加预约提醒")
    change = await act(client, alice, change, "submit")
    for kind in ("task", "acceptance"):
        row = await draft(client, admin, a["id"], kind, revision=1)
        await act(client, admin, row, "submit")
    row = await draft(client, alice, a["id"], "bug", revision=1)
    await act(client, alice, row, "submit")
    for identity in (alice, bob):
        for path in (
            "/api/admin/projects",
            "/api/admin/projects/summary",
            "/api/admin/projects/items?category=documents",
        ):
            assert (await client.get(path, headers=identity)).status_code == 403
    summary = (await client.get("/api/admin/projects/summary", headers=admin)).json()
    assert summary == {
        "consultations": 1,
        "projects": 1,
        "pending_reviews": 1,
        "active_tasks": 1,
        "active_deliveries": 1,
        "open_service": 1,
    }
    page = (await client.get("/api/admin/projects?limit=1", headers=admin)).json()
    assert len(page["items"]) == 1 and page["has_more"]
    result = (await client.get("/api/admin/projects?phase=project", headers=admin)).json()
    assert [row["id"] for row in result["items"]] == [a["id"]]
    assert result["items"][0]["customer_username"] == "alice"
    result = (await client.get("/api/admin/projects?q=bob", headers=admin)).json()
    assert [row["id"] for row in result["items"]] == [b["id"]]
    assert not (await client.get("/api/admin/projects?q=%25", headers=admin)).json()["items"]
    queue = (
        await client.get(
            "/api/admin/projects/items?category=documents&status=pending_review", headers=admin
        )
    ).json()
    assert [row["id"] for row in queue["items"]] == [change["id"]]
    assert queue["items"][0]["customer_username"] == "alice"
    assert "content" not in queue["items"][0]
    assert queue["items"][0]["project_title"]
    await act(client, admin, change, "confirm")
    queue = (
        await client.get(
            "/api/admin/projects/items?category=documents&status=pending_review", headers=admin
        )
    ).json()
    assert not queue["items"]
    assert (
        await client.get("/api/admin/projects/items?category=unknown", headers=admin)
    ).status_code == 422
    assert (await client.get("/api/admin/projects?limit=101", headers=admin)).status_code == 422


async def test_admin_workspace_excludes_recycled_projects_and_restores_counts(auth_api):
    client, _, _ = auth_api
    customer, admin = headers(await login(client)), headers(await login(client, "admin"))
    p = await project(client, customer)
    doc = await draft(client, customer, p["id"])
    await act(client, customer, doc, "submit")
    current = (await client.get(f"/api/projects/{p['id']}", headers=admin)).json()["project"]
    for action, expected in (("trash", 0), ("restore", 1)):
        response = await client.post(
            "/api/projects/records/actions",
            headers=admin,
            json={
                "request_id": str(uuid4()),
                "action": action,
                "targets": [{"id": p["id"], "expected_version": current["version"]}],
            },
        )
        assert response.status_code == 200
        current = response.json()["items"][0]
        summary = (await client.get("/api/admin/projects/summary", headers=admin)).json()
        assert summary["consultations"] == expected and summary["pending_reviews"] == expected
        assert (
            len((await client.get("/api/admin/projects", headers=admin)).json()["items"])
            == expected
        )
        assert (
            len(
                (
                    await client.get("/api/admin/projects/items?category=documents", headers=admin)
                ).json()["items"]
            )
            == expected
        )

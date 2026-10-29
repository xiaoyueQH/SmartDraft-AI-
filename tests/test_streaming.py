# ruff: noqa: F811
import asyncio
import json
from uuid import UUID, uuid4

from test_auth import auth_api, headers, login  # noqa: F401
from test_projects import project

from backend.agents.runtime import AgentRuntime
from backend.models.projects import ProjectError


async def test_question_filter_also_bounds_streaming_preview(auth_api):
    from backend.agents.consultation.state import limit_questions
    from backend.db.project_store import ProjectStore

    client, _, database = auth_api
    runtime = client._transport.app.state.agent_runtime
    store = ProjectStore(database)
    rid = uuid4()
    write = await runtime.stream_writer(store, rid, transform=lambda text: limit_questions(text)[0])
    for delta in ("先梳理。谁接单？", "怎么分派？", "什么界面？", "什么语言？"):
        await write(delta)
        preview = await runtime.preview(store, rid)
        assert preview["text"].count("？") <= 2
    assert "什么界面" not in preview["text"] and "什么语言" not in preview["text"]


def frames(response):
    return [
        (frame.splitlines()[0], json.loads(frame.split("data: ", 1)[1]))
        for frame in response.text.strip().split("\n\n")
    ]


async def test_authenticated_stream_shows_preview_then_authoritative_saved_reply(
    auth_api, monkeypatch
):
    client, _, _ = auth_api
    alice, bob = headers(await login(client)), headers(await login(client, "bob"))
    p = await project(client, alice)
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        await kwargs["on_text"]("先从预约")
        await asyncio.sleep(0.25)
        await kwargs["on_text"]("登记开始。")
        await asyncio.sleep(0.25)
        return {"content": "先从预约登记开始。"}, {"total_tokens": 5}

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    accepted = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=alice,
        json={"request_id": str(uuid4()), "content": "可以帮我解释吗"},
    )
    rid = accepted.json()["id"]
    path = f"/api/projects/{p['id']}/runs/{rid}/stream"
    assert (await client.get(path, headers=bob)).status_code == 404
    result = await client.get(path, headers=alice)
    assert result.status_code == 200 and result.headers["x-accel-buffering"] == "no"
    updates = [value for _, value in frames(result)]
    assert any(
        row.get("preview", {}).get("text") == "先从预约" for row in updates if row.get("preview")
    )
    assert updates[-1]["status"] == "completed" and updates[-1]["preview"] is None
    detail = (await client.get(f"/api/projects/{p['id']}", headers=alice)).json()
    assert (
        len(detail["messages"]) == 2 and detail["messages"][-1]["content"] == "先从预约登记开始。"
    )
    assert not client._transport.app.state.agent_runtime.stream_viewers


async def test_stream_rechecks_revocation_without_occupying_regular_request_slots(
    auth_api, monkeypatch
):
    client, service, _ = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    p = await project(client, identity)
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))
    waiting = asyncio.Event()

    async def complete(self, messages, **kwargs):
        await kwargs["on_text"]("正在整理")
        await waiting.wait()
        raise ProjectError("TEST_CANCELLED", "测试结束", 503)

    monkeypatch.setattr(AgentRuntime, "complete", complete)
    accepted = await client.post(
        f"/api/projects/{p['id']}/messages",
        headers=identity,
        json={"request_id": str(uuid4()), "content": "请解释流程"},
    )
    rid = accepted.json()["id"]
    app = client._transport.app
    app.state.request_slots = asyncio.Semaphore(1)
    connection = asyncio.create_task(
        client.get(f"/api/projects/{p['id']}/runs/{rid}/stream", headers=identity)
    )
    try:
        await asyncio.sleep(0.25)
        assert (await client.get("/api/auth/me", headers=identity)).status_code == 200
        await service.store.revoke(UUID(tokens["user"]["id"]), UUID(tokens["session_id"]))
        response = await asyncio.wait_for(connection, 3)
        assert frames(response)[-1][1]["status"] == 401
        assert not app.state.agent_runtime.stream_viewers
        assert app.state.request_slots._value == 1
    finally:
        waiting.set()
        await asyncio.gather(*app.state.agent_runtime.tasks)


async def test_stream_never_started_does_not_reserve_a_viewer(auth_api):
    from starlette.requests import Request

    from backend.api.v1.projects import stream_run
    from backend.db.project_store import ProjectStore
    from backend.models.projects import SendMessage

    client, service, database = auth_api
    tokens = await login(client)
    identity = headers(tokens)
    p = await project(client, identity)
    principal = await service.authenticate(tokens["access_token"])
    store = ProjectStore(database)
    run, _ = await store.start_run(
        principal, UUID(p["id"]), SendMessage(request_id=uuid4(), content="先登记")
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "app": client._transport.app,
            "headers": [(b"authorization", identity["Authorization"].encode())],
        }
    )
    response = await stream_run(UUID(p["id"]), UUID(run["id"]), request, principal, store)
    await response.body_iterator.aclose()
    assert not request.app.state.agent_runtime.stream_viewers


async def test_accidental_json_reply_is_not_published_as_customer_text():
    from types import SimpleNamespace

    from backend.config import AgentSettings

    runtime = AgentRuntime(AgentSettings())
    store = SimpleNamespace(cache=None)
    run_id = uuid4()
    try:
        write = await runtime.stream_writer(store, run_id)
        await write('{"reply":')
        await write('"internal-format"}')
        assert (await runtime.preview(store, run_id))["text"] == ""
        write = await runtime.stream_writer(store, run_id)
        await write("先从登记开始。")
        assert (await runtime.preview(store, run_id))["text"] == "先从登记开始。"
    finally:
        await runtime.close()

# ruff: noqa: F811
from uuid import uuid4

import httpx
import pytest
from test_auth import auth_api, headers, login  # noqa: F401

from backend.agents.tools.web_search import public_url, search_public


async def test_tool_control_is_admin_only_versioned_and_idempotent(auth_api):
    client, _, _ = auth_api
    user, admin = headers(await login(client)), headers(await login(client, "admin"))
    initial = (await client.get("/api/agent/tools", headers=user)).json()["web_search"]
    assert initial["enabled"] is False
    command = {"request_id": str(uuid4()), "enabled": True, "expected_version": initial["version"]}
    assert (
        await client.post("/api/admin/agent/tools/web-search", headers=user, json=command)
    ).status_code == 403
    response = await client.post("/api/admin/agent/tools/web-search", headers=admin, json=command)
    assert response.status_code == 200 and response.json()["enabled"]
    assert (
        await client.post("/api/admin/agent/tools/web-search", headers=admin, json=command)
    ).json() == response.json()
    assert (
        await client.post(
            "/api/admin/agent/tools/web-search", headers=admin, json={**command, "enabled": False}
        )
    ).status_code == 409
    assert (
        await client.post(
            "/api/admin/agent/tools/web-search",
            headers=admin,
            json={**command, "request_id": str(uuid4())},
        )
    ).status_code == 409


async def test_duckduckgo_adapter_filters_private_links_and_sensitive_queries():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                '<a class="result__a" href="//duckduckgo.com/l/?'
                'uddg=https%3A%2F%2Fexample.org%2Fdocs">官方资料</a>'
                '<a class="result__snippet">接口说明</a>'
                '<a class="result__a" href="http://127.0.0.1/admin">私有地址</a>'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rows = await search_public(client, "public booking docs")
        assert len(rows) == 1 and rows[0]["url"] == "https://example.org/docs"
        assert rows[0]["snippet"] == "接口说明"
        assert requests[0].url.host == "html.duckduckgo.com"
        with pytest.raises(ValueError):
            await search_public(client, "customer@example.org")
        assert len(requests) == 1
    assert public_url("javascript:alert(1)") is None
    assert public_url("http://192.168.1.1/private") is None


async def test_search_challenge_is_not_parsed_or_bypassed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(202, text="challenge"))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await search_public(client, "booking docs")

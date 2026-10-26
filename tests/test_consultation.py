# ruff: noqa: F811
import json
from uuid import uuid4

import pytest
from drafting_support import DraftModel
from test_auth import auth_api, headers, login  # noqa: F401
from test_postgresql import postgres, postgres_auth  # noqa: F401
from test_projects import project, wait_run

from backend.agents.runtime import AgentRuntime


def install_model(monkeypatch, model):
    monkeypatch.setattr(AgentRuntime, "configured", property(lambda self: True))

    async def complete(self, messages, **kwargs):
        return await model.respond(messages)

    monkeypatch.setattr(AgentRuntime, "complete", complete)


def test_source_catalog_resolves_only_customer_evidence():
    from backend.agents.consultation.state import source_catalog, verified_patch
    from backend.models.consultation import Extraction

    history = [
        {"id": "old-user", "role": "user", "content": "前台负责登记", "run_id": "old-run"},
        {"id": "assistant", "role": "assistant", "content": "建议增加营销"},
    ]
    catalog = source_catalog("只做预约", "current-run", history)
    assert {row["id"] for row in catalog} == {"current:0", "old-user:0"}
    extraction = Extraction.model_validate(
        {
            "notes": {"users": ["前台"], "scope": ["预约"]},
            "sources": [
                {"field": "users", "value": "前台", "source_id": "old-user:0"},
                {"field": "scope", "value": "预约", "source_id": "current:0"},
            ],
            "reply": "已记录",
        }
    )
    patch, sources = verified_patch(extraction, "只做预约", "current-run", {}, history)
    assert patch == {"users": ["前台"], "scope": ["预约"]}
    assert (sources[0].quote, sources[0].message_id, sources[0].run_id) == (
        "前台负责登记",
        "old-user",
        "old-run",
    )
    assert (sources[1].quote, sources[1].message_id, sources[1].run_id) == (
        "只做预约",
        "",
        "current-run",
    )
    for invalid_id in ("assistant:0", "invented:0"):
        extraction.sources[0].source_id = invalid_id
        with pytest.raises(ValueError, match="source_id不存在"):
            verified_patch(extraction, "只做预约", "current-run", {}, history)


def test_source_catalog_has_a_bounded_context():
    from backend.agents.consultation.state import source_catalog

    catalog = source_catalog("客户原话" * 4000, "run")
    assert sum(len(row["text"]) for row in catalog) <= 12000
    assert all(len(row["text"]) <= 500 for row in catalog)


def test_exact_combined_list_sources_expand_without_accepting_fabricated_origins():
    from backend.agents.consultation.state import verified_patch
    from backend.models.consultation import Extraction

    extraction = Extraction.model_validate(
        {
            "notes": {"users": ["管理员", "仓库人员"]},
            "sources": [{"field": "users", "value": "管理员, 仓库人员", "source_id": "current:0"}],
            "reply": "已了解",
        }
    )
    _, sources = verified_patch(extraction, "管理员和仓库人员使用", "run", {})
    assert [source.value for source in sources] == ["管理员", "仓库人员"]
    assert all(source.quote == "管理员和仓库人员使用" for source in sources)
    extraction.sources[0].source_id = "invented"
    with pytest.raises(ValueError, match="source_id不存在"):
        verified_patch(extraction, "管理员和仓库人员使用", "run", {})
    extraction.sources[0].source_id = "current:0"
    extraction.sources[0].value = "管理员, 其他人"
    with pytest.raises(ValueError, match="缺少匹配"):
        verified_patch(extraction, "管理员和仓库人员使用", "run", {})


def extracted(prompt, notes=None, **extra):
    notes = notes or {}
    sources = []
    for field, value in notes.items():
        if field == "open_questions":
            continue
        for item in value if isinstance(value, list) else [value]:
            sources.append({"field": field, "value": item, "quote": prompt})
    payload = {
        "notes": notes,
        "sources": sources,
        "reply": "已了解您的业务，接下来希望确认主要使用人员。",
        **extra,
    }
    return {
        "tool_calls": [
            {
                "id": "record",
                "type": "function",
                "function": {
                    "name": "record_consultation",
                    "arguments": json.dumps(payload, ensure_ascii=False),
                },
            }
        ]
    }, {"total_tokens": 100}


async def send(client, identity, pid, content, **options):
    response = await client.post(
        f"/api/projects/{pid}/messages",
        headers=identity,
        json={"request_id": str(uuid4()), "content": content, **options},
    )
    assert response.status_code == 202, response.text
    return await wait_run(client, identity, pid, response.json()["id"])


async def test_collection_does_not_start_draft_until_customer_requests(auth_api, monkeypatch):
    client, _, _ = auth_api
    model = DraftModel()
    model.ready = True
    install_model(monkeypatch, model)
    identity = headers(await login(client))
    p = await project(client, identity)
    first = await send(client, identity, p["id"], "手工录单，确认支付后扣库存，没有运费填0。")
    assert first["status"] == "completed"
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert detail["project"]["discovery"]["scope"] and not detail["items"]
    before = len(model.calls)
    drafted = await send(client, identity, p["id"], "帮我生成一份草案吧")
    assert drafted["status"] == "completed", drafted
    assert len(model.calls) == before + 1 and "DRAFT_WRITE" in model.calls[-1]
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(detail["items"]) == 1 and len(detail["messages"]) == 4
    assert detail["project"]["phase"] == "discovery" and detail["project"]["revision"] == 0
    assert "确认支付后扣库存" not in detail["project"]["discovery"].get("scope", [])
    before = len(model.calls)
    await send(client, identity, p["id"], "谢谢")
    assert len(model.calls) == before + 1
    detail = (await client.get(f"/api/projects/{p['id']}", headers=identity)).json()
    assert len(detail["items"]) == 1


async def test_explicit_request_does_not_ask_for_payment_api_documents(auth_api, monkeypatch):
    client, _, _ = auth_api
    model = DraftModel()
    install_model(monkeypatch, model)
    identity = headers(await login(client))
    p = await project(client, identity)
    await send(
        client,
        identity,
        p["id"],
        "做宠物用品销售，小程序支付，手工订单和库存管理，接口资质还不知道。",
    )
    result = await send(client, identity, p["id"], "帮我生成一份草案吧")
    assert result["status"] == "completed" and "已根据保存" in result["answer"]
    assert not any(marker in result["answer"] for marker in ("请提供", "技术支持联系方式"))
    assert sum("DRAFT_WRITE" in call for call in model.calls) == 1

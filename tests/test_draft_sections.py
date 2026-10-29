import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agents.drafting.graph import create_draft
from backend.agents.drafting.nodes import DraftNodes
from backend.agents.drafting.state import DraftState
from backend.models.consultation import ConsultationState
from backend.models.drafting import DraftPlan
from backend.models.projects import ProjectError

SPECS = [
    {"id": "goals", "title": "业务目标", "focus": "本期范围和目标"},
    {"id": "functions", "title": "角色与功能", "focus": "权限、流程和业务规则"},
]


def call(name, args):
    return {
        "id": name + str(len(json.dumps(args))),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def plan_calls():
    return [call("plan_draft", {"sections": SPECS, "role_terms": ["店主", "员工"]})]


def tools(*calls):
    return {"tool_calls": list(calls)}


class AgentFlow:
    def __init__(self, script=(), *, fail_section=None, web=False):
        self.notes = {
            "scope": ["订单登记", "库存查询"],
            "users": ["店主", "员工"],
            "constraints": ["员工不能退款"],
        }
        self.state = ConsultationState()
        self.snapshot = {"project": {"version": 1, "title": "业务管理"}}
        self.options = {"web_search": web}
        self.rounds, self.calls, self.tokens = 0, 0, 0
        self.agent = SimpleNamespace(
            settings=SimpleNamespace(
                draft_history_chars=200000,
                draft_run_seconds=150,
                draft_model_timeout_seconds=60,
                draft_section_output_tokens=2400,
                draft_max_rounds=8,
                draft_max_tools=8,
                draft_token_budget=64000,
            ),
            client=object(),
        )
        self.principal, self.project_id, self.run_id = "owner", "project", "run"
        self.store = SimpleNamespace(
            database=None,
            draft_history=self.history,
            event=self.event_record,
            draft_target=self.target,
        )
        self.models, self.events, self.script, self.section_writes = [], [], list(script), []
        self.fail_section = fail_section

    async def history(self, *args):
        return []

    async def target(self, *args):
        return None

    async def event_record(self, run_id, event):
        self.events.append(event)

    async def event(self, node, summary, **extra):
        self.events.append({"node": node, "summary": summary, **extra})

    async def model(self, system, data, **kwargs):
        self.models.append((system, data, copy.deepcopy(kwargs)))
        self.rounds += 1
        if "DRAFT_SECTION" in system:
            sid = data["current_section"]["id"]
            self.section_writes.append(sid)
            if sid == self.fail_section:
                raise TimeoutError("private provider details")
            return {
                "content": f"### {sid}\n" + "【已说明】店主确认收款后扣库存，员工无退款权限。" * 6
            }
        response = self.script.pop(0)
        if kwargs.get("count_native_tools", True):
            self.calls += len(response.get("tool_calls") or [])
        return response


async def test_agent_observes_tool_results_then_selects_next_action():
    flow = AgentFlow(
        [
            tools(*plan_calls(), call("write_prd_section", SPECS[1])),
            tools(call("read_draft", {})),
            tools(
                call("write_prd_section", SPECS[0]),
                call("finish_draft", {"questions": ["导入格式待确认"]}),
            ),
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and flow.section_writes == ["functions", "goals"]
    conversations = [
        kwargs["conversation"] for system, _, kwargs in flow.models if "DRAFT_AGENT" in system
    ]
    assert any(m["role"] == "tool" and json.loads(m["content"])["ok"] for m in conversations[-1])
    assert not any("plan_draft" in str(m) for m in conversations[-1])
    assert "导入格式待确认" in result["document"]["questions"]
    assert "员工不能退款" in result["document"]["content"]
    written = [data for system, data, _ in flow.models if "DRAFT_SECTION" in system]
    assert all(
        kwargs["purpose"] == ("draft_writer" if "DRAFT_SECTION" in system else "draft_planner")
        for system, _, kwargs in flow.models
    )
    assert "店主确认收款后扣库存" in written[1]["written_sections"]["functions"]["excerpt"]
    assert written[0]["written_sections"] == {}
    decisions = [(system, data) for system, data, _ in flow.models if "DRAFT_AGENT" in system]
    assert all(data["remaining_budget"]["tokens"] == 64000 for _, data in decisions)
    assert len(decisions[1][0].encode("utf-8")) < len(decisions[0][0].encode("utf-8")) // 2


async def test_model_can_choose_one_shot_without_program_size_branch():
    body = "# 需求草案\n\n## 业务范围\n" + "订单登记和库存查询按已说明岗位分工实施。" * 9
    flow = AgentFlow([{"content": body}])
    flow.notes["scope"] = [f"功能{i}" for i in range(20)]
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and len(flow.models) == 1
    assert "write_prd_section" in str(flow.models[0][2]["tools"])


async def test_section_failure_returns_observation_and_retains_other_sections():
    flow = AgentFlow(
        [
            tools(*plan_calls(), call("write_prd_section", SPECS[0])),
            tools(call("write_prd_section", SPECS[1])),
            tools(call("finish_draft", {})),
        ],
        fail_section="functions",
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and len(flow.models) == 5
    assert "本章暂未生成" in result["document"]["content"]
    assert "private" not in str(result) + str(flow.events)
    assert any("角色与功能" in q for q in result["document"]["questions"])
    assert any(
        m["role"] == "tool" and json.loads(m["content"])["ok"] is False
        for m in flow.models[-1][2]["conversation"]
    )


async def test_duplicate_write_does_not_repeat_model_work():
    flow = AgentFlow(
        [
            tools(call("write_prd_section", SPECS[0])),
            tools(call("write_prd_section", SPECS[0]), call("finish_draft", {})),
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and flow.section_writes == ["goals"]


async def test_completed_outline_finalizes_without_another_decision_or_duplicate_write():
    flow = AgentFlow(
        [
            tools(*plan_calls(), call("write_prd_section", SPECS[0])),
            tools(call("write_prd_section", SPECS[1])),
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"]
    assert flow.section_writes == ["goals", "functions"] and flow.rounds == 4
    assert flow.calls == 3


async def test_finishing_early_keeps_missing_outline_sections_as_partial():
    flow = AgentFlow(
        [tools(*plan_calls(), call("write_prd_section", SPECS[0]), call("finish_draft", {}))]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and "本章暂未生成" in result["document"]["content"]


async def test_model_failure_keeps_already_written_chapters():
    flow = AgentFlow([tools(call("write_prd_section", SPECS[0]))])
    original = flow.model

    async def model(system, data, **kwargs):
        if flow.rounds == 2:
            raise ProjectError("TOKEN_BUDGET", "预算不足", 503)
        return await original(system, data, **kwargs)

    flow.model = model
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and len(flow.models) == 2
    assert "店主确认收款后扣库存" in result["document"]["content"]


async def test_state_and_facts_are_isolated_between_concurrent_invocations():
    first, second = AgentFlow(), AgentFlow()
    first.notes["scope"] = ["甲的业务"]
    second.notes["scope"] = ["乙的业务"]
    a, b = (
        DraftState(business_notes=first.notes).model_copy(deep=True),
        DraftState(business_notes=second.notes).model_copy(deep=True),
    )
    one, two = DraftNodes(first, a), DraftNodes(second, b)
    await asyncio.gather(
        one.toolset.call(call("write_prd_section", SPECS[0])["function"], False),
        two.toolset.call(call("write_prd_section", SPECS[1])["function"], False),
    )
    assert set(a.sections) == {"goals"} and set(b.sections) == {"functions"}
    a.business_notes["scope"].append("局部修改")
    assert first.notes["scope"] == ["甲的业务"] and second.notes["scope"] == ["乙的业务"]
    assert DraftState.model_validate_json(a.model_dump_json()).sections == a.sections


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancellation_and_permission_revocation_are_not_partial_success(cancel):
    flow = AgentFlow()

    async def model(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise ProjectError("FORBIDDEN", "权限已撤销", 403)

    flow.model = model
    with pytest.raises(asyncio.CancelledError if cancel else ProjectError):
        await create_draft(flow, checkpointer=InMemorySaver())


@pytest.mark.parametrize("enabled", [False, True])
async def test_search_can_be_selected_after_writing_and_admin_switch_is_rechecked(
    monkeypatch, enabled
):
    from backend.agents.tools import drafting as draft_tools

    flow = AgentFlow(
        [
            tools(call("write_prd_section", SPECS[0])),
            tools(call("web_search", {"topic": "permissions"})),
            tools(call("write_prd_section", SPECS[1]), call("finish_draft", {})),
        ],
        web=True,
    )
    switches, queries = [], []

    async def get(*args):
        switches.append(1)
        return {"enabled": True if len(switches) == 1 else enabled}

    async def search(client, query, **kwargs):
        queries.append(query)
        return [
            {
                "id": "web-public",
                "url": "https://example.org/guide",
                "title": "公开权限指南",
                "snippet": "参考建议",
                "fetched_at": "2026-10-26",
            }
        ]

    monkeypatch.setattr(draft_tools.ToolStore, "get", get)
    monkeypatch.setattr(draft_tools, "search_public", search)
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and len(switches) == 2 and len(queries) == int(enabled)
    if enabled:
        assert flow.models[-1][1]["public_references"]
    assert flow.notes["users"] == ["店主", "员工"]
    assert all("店主" not in q and "员工" not in q for q in queries)


async def test_unknown_tool_or_bad_arguments_return_safe_feedback():
    flow = AgentFlow(
        [
            tools(call("run_sql", {"sql": "private"})),
            tools(call("write_prd_section", SPECS[0]), call("finish_draft", {})),
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and "private" not in str(flow.events)
    assert (
        json.loads(flow.models[1][2]["conversation"][1]["content"])["code"]
        == "DRAFT_TOOL_ARGUMENTS"
    )


async def test_deadline_preserves_completed_chapter_and_commit_reserve():
    flow = AgentFlow([tools(call("write_prd_section", SPECS[0]))])
    started = asyncio.get_running_loop().time()
    flow.execution_deadline = started + 13.1
    original = flow.model

    async def model(system, data, **kwargs):
        if flow.rounds == 2:
            await asyncio.sleep(5)
        return await original(system, data, **kwargs)

    flow.model = model
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and asyncio.get_running_loop().time() - started < 2
    assert "店主确认收款后扣库存" in result["document"]["content"]


def test_outline_validation_rejects_duplicate_section_ids():
    with pytest.raises(ValueError):
        DraftPlan(sections=[SPECS[0], SPECS[0]])


async def test_oversized_native_batch_keeps_completed_actions_within_actual_tool_budget():
    flow = AgentFlow(
        [
            tools(
                call("write_prd_section", SPECS[0]),
                call("read_draft", {}),
                call("read_draft", {}),
                call("read_draft", {}),
            )
        ]
    )
    flow.agent.settings.draft_max_tools = 2
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and flow.calls == 2
    assert flow.section_writes == ["goals"]
    assert "店主确认收款后扣库存" in result["document"]["content"]
    assert any(event.get("code") == "TOOL_BUDGET" for event in flow.events)


async def test_local_finalization_succeeds_after_processing_budget_is_fully_used():
    flow = AgentFlow(
        [
            tools(
                *plan_calls(),
                call("write_prd_section", {"section_id": "goals"}),
                call("write_prd_section", {"section_id": "functions"}),
                call("finish_draft", {"questions": ["外部邮件账号需接入"]}),
                call("write_prd_section", {"section_id": "extra", "title": "不应执行"}),
            )
        ]
    )
    flow.agent.settings.draft_max_tools = 3
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and flow.calls == 3
    assert flow.section_writes == ["goals", "functions"]
    assert result["document"]["questions"] == ["外部邮件账号需接入"]
    assert not any(event.get("code") == "TOOL_BUDGET" for event in flow.events)


async def test_invalid_finalization_does_not_grant_unlimited_free_tool_actions():
    flow = AgentFlow(
        [
            tools(
                call("write_prd_section", SPECS[0]),
                call("finish_draft", {"questions": "invalid"}),
                call("finish_draft", {}),
            )
        ]
    )
    flow.agent.settings.draft_max_tools = 1
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert result["partial"] and flow.calls == 1
    finish_events = [e for e in flow.events if e.get("tool") == "finish_draft"]
    assert len(finish_events) == 1 and finish_events[0]["ok"] is False


async def test_write_reads_canonical_outline_by_id_without_repeating_title_and_focus():
    flow = AgentFlow(
        [
            tools(*plan_calls(), call("write_prd_section", {"section_id": "goals"})),
            tools(
                call(
                    "write_prd_section",
                    {"section_id": "functions", "title": "模型改写的章名", "focus": "不同措辞"},
                ),
                call("finish_draft", {}),
            ),
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and flow.section_writes == ["goals", "functions"]
    writes = [data for system, data, _ in flow.models if "DRAFT_SECTION" in system]
    assert writes[1]["current_section"]["title"] == SPECS[1]["title"]
    assert "模型改写的章名" not in result["document"]["content"]


def test_outline_accepts_titles_and_assigns_runtime_section_ids():
    plan = DraftPlan(sections=[{"title": "业务功能"}, {"title": "技术选型"}])
    assert [s.id for s in plan.sections] == ["section-1", "section-2"]
    assert plan.sections[1].focus == "技术选型"


@pytest.mark.parametrize("section_id", ["1", "section_1", "section-1", "业务目标"])
async def test_writer_resolves_local_outline_display_ids_without_rewriting_facts(section_id):
    flow = AgentFlow(
        [
            tools(
                *plan_calls(),
                call("write_prd_section", {"section_id": section_id}),
                call("finish_draft", {}),
            )
        ]
    )
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert flow.section_writes == ["goals"]
    assert "业务目标" in result["document"]["content"]


async def test_auto_choices_are_saved_as_proposals_not_customer_facts():
    assumption = {
        "category": "technology",
        "topic": "技术选型",
        "value": "推荐Python FastAPI、Vue和PostgreSQL",
        "rationale": "小团队维护，异步接口和关系数据适合预约业务",
    }
    flow = AgentFlow(
        [
            tools(
                call(
                    "plan_draft",
                    {
                        "sections": SPECS,
                        "assumptions": [assumption],
                        "resolved_questions": ["使用什么技术栈？"],
                    },
                ),
                call("write_prd_section", {"section_id": "goals"}),
            ),
            tools(call("write_prd_section", {"section_id": "functions"}), call("finish_draft", {})),
        ]
    )
    flow.notes["open_questions"] = ["使用什么技术栈？"]
    original_notes = copy.deepcopy(flow.notes)
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"] and flow.state.draft_assumptions[0].value == assumption["value"]
    assert "自动补充方案（可调整）" in result["document"]["content"]
    assert "【自动生成】" + assumption["value"] in result["document"]["content"]
    assert "使用什么技术栈？" not in result["document"]["questions"]
    assert "technology_preference" not in flow.notes
    assert flow.notes == original_notes
    assert result["document"]["questions"] == []


async def test_explicit_customer_stack_removes_old_auto_technology_from_context():
    from backend.models.drafting import DraftAssumption

    flow = AgentFlow(
        [
            {
                "content": "# 需求草案\n\n## 技术方案\n"
                + "采用客户指定的Java与Spring Boot，实现客户要求的预约流程。" * 6
            }
        ]
    )
    flow.state.draft_assumptions = [
        DraftAssumption(category="technology", topic="后端技术", value="Python FastAPI"),
        DraftAssumption(category="operations", topic="邮件实现", value="Node.js Nodemailer"),
    ]
    flow.notes["technology_preference"] = "Java和Spring Boot"

    async def customer_history(*args):
        return [{"role": "user", "content": "技术选型我明确选择Java和Spring Boot"}]

    flow.store.draft_history = customer_history
    result = await create_draft(flow, checkpointer=InMemorySaver())
    assert not result["partial"]
    context = flow.models[0][1]
    assert context["previous_assumptions"] == []
    assert context["business_notes"]["technology_preference"] == "Java和Spring Boot"
    assert "Python FastAPI" not in result["document"]["content"]
    facts = result["document"]["content"].split("## 客户已说明的信息", 1)[1]
    assert "Java和Spring Boot" in facts and "【自动生成】" not in facts


@pytest.mark.parametrize("failure", [False, True])
async def test_revision_changes_only_the_requested_existing_chapter(failure):
    from types import MethodType

    original = (
        "# 草案\n\n## 业务规则\n必须提前2小时；原细节与列表全部保留。\n\n"
        "## 技术部署\n> 本章暂未生成，请补充细节。\n\n"
        "## 验收\n取消后释放；原验收条目保持不变。"
    )
    flow = AgentFlow()
    flow.prompt = "请把草案里技术部署那一章补齐，然后更新草案。"
    flow.snapshot["items"] = [
        {
            "id": "original",
            "kind": "specification",
            "status": "draft",
            "content": original,
            "questions": [
                "“技术部署”章节尚需补充细节。",
                "实际账号导入方式待客户决定",
                "详细功能、异常处理和验收方式尚需完善。",
            ],
            "title": "会议室草案",
            "version": 3,
            "created_at": "2026-10-27",
        }
    ]
    base_model = flow.model

    async def draft_target(*args):
        return flow.snapshot["items"][0]

    flow.store.draft_target = draft_target

    async def model(self, system, data, **kwargs):
        if "DRAFT_AGENT" in system:
            selected = next(b for b in data["existing_sections"] if b["title"] == "技术部署")
            if failure:
                self.fail_section = selected["id"]
            return tools(
                call("write_prd_section", {"section_id": selected["id"]}), call("finish_draft", {})
            )
        return await base_model(system, data, **kwargs)

    flow.model = MethodType(model, flow)
    if failure:
        with pytest.raises(ProjectError, match="没有完成章节补写"):
            await create_draft(flow, checkpointer=InMemorySaver())
    else:
        result = await create_draft(flow, checkpointer=InMemorySaver())
        assert result["replacement"]["id"] == "original"
        assert result["document"]["content"].startswith(original.split("## 技术部署")[0])
        assert result["document"]["content"].endswith(original.split("## 验收")[1])
        assert "> 本章暂未生成" not in result["document"]["content"]
        assert result["document"]["questions"] == ["实际账号导入方式待客户决定"]
    assert flow.snapshot["items"][0]["content"] == original


def test_written_chapter_context_fits_budget_without_shortening_saved_document():
    from backend.agents.common import request_budget, tool_json
    from backend.models.drafting import DraftSection
    from backend.models.projects import ProjectError

    state = DraftState(business_notes={"subject": "报修系统"})
    for i in range(3):
        sid = f"s{i}"
        state.section_specs[sid] = DraftSection(id=sid, title=f"章节{i}")
        state.sections[sid] = (
            "开头权限规则。" + "已保存的详细字段和验收条件。" * 700 + "结尾接口约束。"
        )
    full = dict(state.sections)
    before = state.context(include_written=True)
    for sid, section in before["written_sections"].items():
        body = state.sections[sid]
        section["excerpt"] = body[:400] + "\n…\n" + body[-800:]
    with pytest.raises(ProjectError, match="剩余用量不足"):
        request_budget([{"role": "user", "content": tool_json(before)}], False, 9000)
    after = state.context(include_written=True)
    _, allowance = request_budget([{"role": "user", "content": tool_json(after)}], False, 9000)
    assert allowance >= 256
    assert state.sections == full
    assert all(
        "开头权限规则" in s["excerpt"] and "结尾接口约束" in s["excerpt"]
        for s in after["written_sections"].values()
    )


def test_latest_correction_quarantines_old_technical_constraints_before_drafting():
    state = DraftState(
        business_notes={
            "technology_preference": "Java",
            "constraints": ["禁止Java和Python", "不收费"],
        },
        customer_statements=["使用Java", "纠正：Java和Python只是举例，不是我的选择，技术你推荐。"],
    )
    context = state.context()
    assert "technology_preference" not in context["business_notes"]
    assert context["business_notes"]["constraints"] == ["不收费"]
    assert context["unverified_historical_notes"]["constraints"] == ["禁止Java和Python"]
    assert state.business_notes["technology_preference"] == "Java"


async def test_four_chapters_fit_real_input_reservations_and_preserve_detail():
    from backend.agents.common import request_budget, tool_json
    from backend.agents.drafting.graph import run_graph

    class BudgetFlow(AgentFlow):
        async def model(self, system, data, **kwargs):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": "以下为业务数据：" + tool_json(data)},
            ]
            messages += kwargs.get("conversation", [])
            reserve, allowance = request_budget(
                messages,
                bool(kwargs.get("tools")),
                self.agent.settings.draft_token_budget - self.tokens,
                tools=kwargs.get("tools"),
                max_output=kwargs.get("output_limit", 4000),
            )
            assert allowance >= 1000
            result = await super().model(system, data, **kwargs)
            self.tokens += reserve + 1000
            return result

    specs = [
        {"id": f"s{i}", "title": title}
        for i, title in enumerate(("目标权限", "业务功能", "技术部署", "异常验收"))
    ]
    flow = BudgetFlow(
        [
            tools(call("plan_draft", {"sections": specs})),
            tools(*(call("write_prd_section", {"section_id": s["id"]}) for s in specs)),
        ]
    )
    flow.agent.settings.draft_max_rounds = 12
    state = DraftState(
        business_notes=flow.notes,
        customer_statements=["员工只看本人记录，技术由你推荐，自动生成方案。"],
    )
    body, partial = await run_graph(DraftNodes(flow, state), checkpointer=InMemorySaver())
    assert not partial and all(s["title"] in body for s in specs)
    assert len(state.sections) == 4 and "本章暂未生成" not in body

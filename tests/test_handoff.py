"""Cross-Agent scope, budget inheritance and old-request regression scenarios."""

# ruff: noqa: F811
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from test_draft_sections import SPECS, AgentFlow, call, tools

from backend.agents.common import route_agent
from backend.agents.consultation.nodes import FactNodes
from backend.agents.consultation.state import document_action, valid_draft_request
from backend.agents.drafting.graph import create_draft
from backend.agents.drafting.nodes import DraftNodes
from backend.agents.drafting.state import DraftState
from backend.agents.project.nodes import ProjectNodes
from backend.agents.review.nodes import ReviewNodes
from backend.config import AgentSettings
from backend.models.handoff import AgentResult, AgentTask, TaskBudget, digest
from backend.models.projects import ProjectError


def root_task(**changes):
    values = {
        "task_id": uuid4(),
        "run_id": uuid4(),
        "project_id": uuid4(),
        "actor_id": uuid4(),
        "session_id": uuid4(),
        "agent": "consultation",
        "action": "chat",
        "project_version": 3,
        "facts_hash": digest({"scope": ["预约"]}),
        "budget": TaskBudget(
            model_calls=8,
            tool_calls=8,
            tokens=64000,
            deadline=datetime.now(UTC) + timedelta(seconds=150),
            save_seconds=12,
        ),
    }
    return AgentTask(**(values | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"jwt": "not-allowed"},
        {"agent": "project", "action": "review"},
        {"action": "record", "allow_search": True},
        {"item_version": 2},
    ],
)
def test_invalid_protocol_or_escalated_capability_is_rejected(changes):
    with pytest.raises(ValidationError):
        root_task(**changes)


def test_child_inherits_scope_remaining_budget_and_cannot_restart_allowance():
    parent = root_task()
    remaining = parent.budget.remaining(models=3, tools=2, tokens=5000)
    child = parent.delegate_document("create", budget=remaining, source_ids=("run:current",))
    assert (child.project_id, child.actor_id, child.session_id) == (
        parent.project_id,
        parent.actor_id,
        parent.session_id,
    )
    assert (child.budget.model_calls, child.budget.tool_calls, child.budget.tokens) == (5, 6, 59000)
    assert child.budget.deadline == parent.budget.deadline
    assert (
        child.task_id
        == parent.delegate_document("create", budget=remaining, source_ids=("run:current",)).task_id
    )
    for field, value in (
        ("model_calls", 9),
        ("tokens", 64001),
        ("deadline", parent.budget.deadline + timedelta(seconds=1)),
        ("save_seconds", 0),
    ):
        budget = TaskBudget.model_validate(parent.budget.model_dump() | {field: value})
        with pytest.raises(ValueError, match="扩大预算"):
            parent.delegate_document("create", budget=budget, source_ids=("run:current",))
    with pytest.raises(ValueError, match="不能交接"):
        root_task(action="record").delegate_document(
            "create", budget=remaining, source_ids=("run:current",)
        )


def test_update_requires_document_snapshot_and_result_cannot_cross_project_or_body():
    parent = root_task()
    with pytest.raises(ValidationError, match="完整文档快照"):
        parent.delegate_document("revise", budget=parent.budget, source_ids=("run:current",))
    child = parent.delegate_document(
        "revise",
        budget=parent.budget,
        source_ids=("run:current",),
        item={"id": str(uuid4()), "version": 2, "content": "## 规则\n预约"},
    )
    result = AgentResult.for_task(child, content="## 规则\n最多2小时")
    result.validate_for(child, content="## 规则\n最多2小时")
    with pytest.raises(ValueError, match="不匹配"):
        result.validate_for(child, content="## 规则\n不限时间")
    with pytest.raises(ValueError, match="不匹配"):
        AgentResult.model_validate(result.model_dump() | {"project_id": uuid4()}).validate_for(
            child, content="## 规则\n最多2小时"
        )


@pytest.mark.parametrize(
    "text",
    [
        "愿意试点，有现成的售后模板吗？",
        "不要生成草案，先聊业务",
        "比如用户说直接生成草案",
        "直接生成草案，算了先不要",
    ],
)
def test_record_only_template_and_cancellation_never_authorize_handoff(text):
    assert not valid_draft_request(text, text)


def test_human_pending_can_finish_but_human_chat_and_silent_branch_cannot_generate():
    assert route_agent("discovery", mode="pending", accepted=True) == "consultation"
    with pytest.raises(ProjectError, match="正在交接人工"):
        route_agent("discovery", mode="pending")
    with pytest.raises(ProjectError, match="人工接待"):
        route_agent("discovery", mode="human")
    assert route_agent("project", mode="human", silent=True) == "consultation"
    assert route_agent("discovery", mode="human", manual_document=True) == "consultation"
    assert route_agent("project") == "project"
    assert route_agent("discovery", review=True, mode="human") == "review"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("补齐需求书的验收章节", "fill"),
        ("更新需求文档的预约规则", "revise"),
        ("重新生成整份需求书", "rewrite_all"),
        ("不要重新生成整份需求书，只更新需求文档的预约规则", "revise"),
    ],
)
def test_authorized_action_preserves_update_vs_rewrite_boundary(text, expected):
    assert valid_draft_request(text, text)
    assert document_action(text, has_draft=True) == expected


def scoped_flow(script=()):
    flow = AgentFlow(script)
    flow.task = root_task()
    flow.project_id, flow.run_id = flow.task.project_id, flow.task.run_id
    flow.principal = SimpleNamespace(
        account=SimpleNamespace(id=flow.task.actor_id), session_id=flow.task.session_id
    )
    flow.prompt, flow.document_action = "生成需求书", "create"
    return flow


async def test_real_draft_graph_returns_bound_result_and_reuses_checkpoint_without_new_calls():
    flow = scoped_flow(
        [
            tools(
                call("write_prd_section", SPECS[0]),
                call("write_prd_section", SPECS[1]),
                call("finish_draft", {}),
            )
        ]
    )
    saver = InMemorySaver()
    result = await create_draft(flow, checkpointer=saver)
    assert not result["partial"] and flow.section_writes == ["goals", "functions"]
    AgentResult.model_validate(result["handoff"]).validate_for(
        flow.document_task, content=result["document"]["content"]
    )
    assert flow.document_task.source_ids == (f"run:{flow.run_id}",)
    first_calls = len(flow.models)
    repeated = await create_draft(flow, checkpointer=saver)
    assert repeated == result and len(flow.models) == first_calls


async def test_foreign_runtime_task_is_refused_before_model_generation():
    flow = scoped_flow()
    flow.task = root_task()
    with pytest.raises(ProjectError) as error:
        await create_draft(flow, checkpointer=InMemorySaver())
    assert error.value.code == "HANDOFF_CONFLICT" and not flow.models


async def test_checkpoint_cannot_replace_scope_and_exhausted_child_cannot_call_model():
    flow = scoped_flow()
    child = flow.task.delegate_document(
        "create", budget=flow.task.budget.remaining(models=8), source_ids=("run:current",)
    )
    state = DraftState(handoff=child, handoff_counters={"models": 0, "tools": 0, "tokens": 0})
    nodes = DraftNodes(flow, state)
    with pytest.raises(ProjectError, match="剩余预算不足"):
        await nodes.model("write", {})
    assert not flow.models
    altered = AgentTask.model_validate(child.model_dump() | {"project_id": uuid4()})
    with pytest.raises(ProjectError) as error:
        nodes.restore(
            {
                "draft": state.model_dump() | {"handoff": altered.model_dump()},
                "counters": {},
                "expires_at": 0,
            }
        )
    assert error.value.code == "HANDOFF_CONFLICT"


async def test_save_reserve_is_applied_once_and_candidate_cannot_claim_a_commit():
    flow = scoped_flow()
    child = flow.task.delegate_document(
        "create", budget=flow.task.budget, source_ids=("run:current",)
    )
    nodes = DraftNodes(
        flow, DraftState(handoff=child, handoff_counters={"models": 0, "tools": 0, "tokens": 0})
    )
    assert 135 < nodes.remaining()["seconds"] <= 138
    result = AgentResult.for_task(child, content="候选", saved_project_version=4)
    with pytest.raises(ValueError, match="不能自报保存回执"):
        result.validate_for(child, content="候选")


async def test_all_business_branches_enforce_a_narrower_handoff_before_calling_model():
    parent = root_task()
    empty = parent.budget.remaining(models=8)
    principal = SimpleNamespace(
        account=SimpleNamespace(id=parent.actor_id), session_id=parent.session_id
    )
    runtime = SimpleNamespace(settings=AgentSettings())
    project = ProjectNodes(runtime, None, principal, parent.project_id, parent.run_id, {})
    project.task = AgentTask.model_validate(
        parent.model_dump() | {"agent": "project", "budget": empty.model_dump()}
    )
    with pytest.raises(ProjectError, match="次数已到上限"):
        await project.decide({"rounds": 0})
    fact = FactNodes(
        runtime,
        None,
        principal,
        parent.project_id,
        {"project": {"version": 3, "discovery": {}}, "messages": [], "run_id": str(parent.run_id)},
    )
    fact.task = AgentTask.model_validate(
        parent.model_dump() | {"action": "record", "budget": empty.model_dump()}
    )
    with pytest.raises(ProjectError, match="次数上限"):
        await fact.model("record", {})
    item_id, review_id = uuid4(), uuid4()
    task = AgentTask.model_validate(
        parent.model_dump()
        | {
            "agent": "review",
            "action": "review",
            "review_id": review_id,
            "run_id": None,
            "item_id": item_id,
            "item_version": 2,
            "content_hash": digest("original"),
            "budget": empty.model_dump(),
        }
    )
    review = ReviewNodes(
        runtime,
        None,
        principal,
        task.project_id,
        item_id,
        review_id,
        {"source": {"version": 2, "hash": task.content_hash, "client_quotes": []}},
        task,
    )
    with pytest.raises(ProjectError, match="预算"):
        await review.model({"rounds": 0, "tokens": 0, "expires_at": time.time() + 100}, [])

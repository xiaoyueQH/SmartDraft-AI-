import time
from datetime import UTC, datetime, timedelta

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from backend.agents.common import route_agent
from backend.agents.review.nodes import ReviewNodes
from backend.agents.review.state import ReviewState
from backend.db.checkpoints import DatabaseCheckpointer
from backend.models.handoff import AgentResult, AgentTask, TaskBudget, digest
from backend.models.projects import ProjectError


def build_graph(nodes):
    graph = StateGraph(ReviewState)
    for name in ("split_blocks", "assess", "verify", "finalize", "wait_user", "apply"):
        graph.add_node(name, getattr(nodes, name))
    graph.add_node("tools", nodes.act)
    graph.add_edge(START, "split_blocks")
    graph.add_edge("split_blocks", "assess")
    graph.add_conditional_edges("assess", lambda s: "tools" if s["pending"] else "verify")
    graph.add_conditional_edges(
        "tools", lambda s: "verify" if s["complete"] else ("tools" if s["pending"] else "assess")
    )
    graph.add_edge("verify", "finalize")
    graph.add_edge("finalize", "wait_user")
    graph.add_edge("wait_user", "apply")
    graph.add_edge("apply", END)
    return graph.compile(checkpointer=DatabaseCheckpointer(nodes.store.database, nodes.project_id))


def config(review_id):
    return {
        "configurable": {"thread_id": f"review:{review_id}", "checkpoint_ns": ""},
        "recursion_limit": 100,
    }


def review_task(runtime, principal, project_id, item_id, review_id, job):
    source = job["source"]
    return AgentTask(
        task_id=review_id,
        project_id=project_id,
        actor_id=principal.account.id,
        session_id=principal.session_id,
        agent=route_agent(None, review=True),
        action="review",
        review_id=review_id,
        item_id=item_id,
        item_version=source["version"],
        content_hash=source["hash"],
        project_version=source["project_version"],
        facts_hash=digest(source["facts"]),
        source_ids=tuple(source.get("initial_source_ids", [])),
        budget=TaskBudget(
            model_calls=runtime.settings.review_max_rounds,
            tool_calls=runtime.settings.review_max_tools,
            tokens=runtime.settings.review_token_budget,
            deadline=datetime.now(UTC) + timedelta(seconds=runtime.settings.review_run_seconds),
            save_seconds=8,
        ),
    )


async def run_review(runtime, store, principal, project_id, item_id, review_id):
    job = await store.load(principal, project_id, item_id, review_id, internal=True)
    task = review_task(runtime, principal, project_id, item_id, review_id, job)
    nodes = ReviewNodes(runtime, store, principal, project_id, item_id, review_id, job, task)
    graph = build_graph(nodes)
    result = await graph.ainvoke(
        {
            "task": task.model_dump(mode="json"),
            "blocks": [],
            "read_ids": [],
            "findings": [],
            "changes": [],
            "messages": [],
            "pending": [],
            "rounds": 0,
            "calls": 0,
            "tokens": 0,
            "complete": False,
            "partial": False,
            "verified": False,
            "summary": "",
            "expires_at": time.time() + task.budget.seconds(),
            "decision": {},
            "result": {},
            "stop_reasons": [],
            "last_invalid_call": "",
            "read_source_ids": job["source"].get("initial_source_ids", []),
            "loaded_sources": [],
        },
        config=config(review_id),
        durability="sync",
    )
    if not result.get("__interrupt__"):
        raise ProjectError("REVIEW_CHECKPOINT", "检查结果尚未就绪，原稿保留", 503)
    await store.ready(review_id)
    return AgentResult.for_task(task, outcome="partial" if result["partial"] else "completed")


async def resume_review(runtime, store, principal, project_id, item_id, review_id, data):
    replay = await store.replay_decision(principal, project_id, item_id, review_id, data)
    if replay:
        return replay
    job = await store.load(principal, project_id, item_id, review_id, internal=True)
    if job["status"] != "ready":
        raise ProjectError("REVIEW_NOT_READY", "检查结果尚未就绪或已处理")
    task = review_task(runtime, principal, project_id, item_id, review_id, job)
    nodes = ReviewNodes(runtime, store, principal, project_id, item_id, review_id, job, task)
    graph = build_graph(nodes)
    previous = await graph.aget_state(config(review_id))
    if previous.values and not previous.values.get("task"):
        # Legacy candidates still have the authoritative PG source snapshot. Upgrade only
        # metadata; the existing apply transaction rechecks ownership, versions and hash.
        await graph.aupdate_state(config(review_id), {"task": task.model_dump(mode="json")})
        previous = await graph.aget_state(config(review_id))
    if previous.values.get("task"):
        original = AgentTask.model_validate(previous.values["task"])
        # A newly authorized actor may apply an existing candidate. Identity is checked by
        # the business transaction; only the immutable document input must stay identical.
        for field in (
            "project_id",
            "item_id",
            "item_version",
            "content_hash",
            "review_id",
            "project_version",
            "facts_hash",
        ):
            if getattr(original, field) != getattr(task, field):
                raise ProjectError("HANDOFF_CONFLICT", "检查资料不匹配，原稿保持不变", 409)
    if previous.next == ("apply",):
        # A rejected version/permission check leaves a resumable apply node, not a new LLM run.
        await graph.aupdate_state(
            config(review_id), {"decision": data.model_dump(mode="json")}, as_node="wait_user"
        )
        result = await graph.ainvoke(None, config=config(review_id), durability="sync")
        return result["result"]
    if previous.next != ("wait_user",):
        raise ProjectError("REVIEW_CHECKPOINT", "检查进度尚未就绪，请稍后重试", 503)
    result = await graph.ainvoke(
        Command(resume=data.model_dump(mode="json")), config=config(review_id), durability="sync"
    )
    return result["result"]

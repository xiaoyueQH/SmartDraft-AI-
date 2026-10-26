from langgraph.graph import START, StateGraph
from langgraph.types import Command

from backend.agents.consultation.nodes import ConsultationNodes
from backend.agents.consultation.state import GraphState
from backend.db.checkpoints import DatabaseCheckpointer


def build_graph(nodes, saver):
    graph = StateGraph(GraphState)
    for name in (
        "extract",
        "decide_draft",
        "save_discussion",
        "draft_agent",
        "wait_user",
        "persist",
        "human_input",
    ):
        graph.add_node(name, nodes.node(name))
    graph.add_edge(START, "extract")
    graph.add_edge("extract", "decide_draft")
    graph.add_conditional_edges(
        "decide_draft",
        lambda state: state["route"],
        {"save_discussion": "save_discussion", "wait_user": "wait_user"},
    )
    graph.add_edge("save_discussion", "draft_agent")
    graph.add_edge("draft_agent", "persist")
    graph.add_edge("wait_user", "persist")
    graph.add_edge("persist", "human_input")
    graph.add_edge("human_input", "extract")
    return graph.compile(checkpointer=saver)


class ConsultationWorkflow(ConsultationNodes):
    async def run(self, prompt):
        self.prompt = prompt
        run = next(item for item in self.snapshot["runs"] if item["id"] == str(self.run_id))
        self.options = run.get("options") or {}
        incoming = {"run_id": str(self.run_id), "prompt": prompt, "options": self.options}
        saver = DatabaseCheckpointer(self.store.database, self.project_id)
        compiled = build_graph(self, saver)
        self.thread = self.state.checkpoint_thread or f"consultation:{self.run_id}"
        config = {"configurable": {"thread_id": self.thread}, "recursion_limit": 20}
        previous = await compiled.aget_state(config)
        if previous.next == ("persist",) and self.state.last_run_id:
            # A committed business receipt can outlive its following checkpoint write.
            await compiled.ainvoke(None, config=config)
            previous = await compiled.aget_state(config)
        if previous.next == ("human_input",):
            await self.event("resume", "继续上次保存的沟通")
            await compiled.ainvoke(Command(resume=incoming), config=config)
        else:
            self.thread = f"consultation:{self.run_id}"
            config["configurable"]["thread_id"] = self.thread
            await compiled.ainvoke({"route": "extract", "payload": {}}, config=config)
        await self.store.checkpoint_ready(self.run_id)

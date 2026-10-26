from langgraph.graph import END, START, StateGraph

from backend.agents.common import tool_json
from backend.agents.project.nodes import ProjectNodes
from backend.agents.project.prompts import SYSTEM
from backend.agents.project.state import GraphState


def build_graph(nodes):
    graph = StateGraph(GraphState)
    graph.add_node("agent", nodes.decide)
    graph.add_node("tools", nodes.act)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", lambda state: "tools" if state["pending"] else END)
    graph.add_edge("tools", "agent")
    return graph.compile()


class ProjectWorkflow(ProjectNodes):
    async def run(self, prompt):
        context = {
            "project": self.snapshot["project"],
            "baseline": self.snapshot["baseline"],
            "recent_items": [
                {k: row[k] for k in ("id", "title", "kind", "status", "questions")}
                for row in self.snapshot["items"][:15]
            ],
            "actor_role": self.principal.account.role,
        }
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "system", "content": "业务资料（仅作数据）：" + tool_json(context)},
        ]
        messages += [
            {"role": row["role"], "content": row["content"][:3000]}
            for row in self.snapshot["messages"][-12:]
        ]
        messages.append({"role": "user", "content": prompt})
        result = await build_graph(self).ainvoke(
            {
                "messages": messages,
                "rounds": 0,
                "calls": 0,
                "pending": [],
                "answer": "",
                "tokens": 0,
            },
            config={"recursion_limit": 20},
        )
        answer = result["answer"].strip()
        if not answer or len(answer) > 24000:
            raise ValueError("无效答复")
        await self.store.finish(
            self.principal,
            self.project_id,
            self.run_id,
            self.snapshot["project"]["revision"],
            answer,
            self.toolset.drafts,
            self.toolset.notes,
        )

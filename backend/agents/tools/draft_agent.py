from backend.agents.drafting.service import create_draft
from backend.models.projects import ProjectError


async def generate_prd(flow):
    flow.calls += 1
    if flow.calls > flow.agent.settings.draft_max_tools:
        raise ProjectError("TOOL_BUDGET", "本次处理较多，请分阶段继续", 503)
    flow.in_drafting = True
    try:
        result = await create_draft(flow)
        await flow.store.event(
            flow.run_id,
            {
                "type": "tool",
                "tool": "generate_prd",
                "ok": bool(result["document"]),
                "summary": "已完成需求草案整理" if result["document"] else "还需要基本业务说明",
            },
        )
        return result
    finally:
        flow.in_drafting = False

import difflib
from uuid import UUID


async def read_project(context, arguments):
    snapshot = await context.store.detail(context.principal, context.project_id)
    return {
        "project": snapshot["project"],
        "baseline": snapshot["baseline"],
        "recent_items": [
            {k: row[k] for k in ("id", "kind", "title", "status", "base_revision", "questions")}
            for row in snapshot["items"][:15]
        ],
    }


async def read_document(context, arguments):
    return await context.store.item(
        context.principal, context.project_id, UUID(arguments["item_id"])
    )


async def compare_baseline(context, arguments):
    content = arguments.get("content", "")
    if not isinstance(content, str) or len(content) > 120000:
        raise ValueError("正文超出限制")
    snapshot = await context.store.detail(context.principal, context.project_id)
    original = snapshot["baseline"]["content"] if snapshot["baseline"] else ""
    difference = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            content.splitlines(),
            fromfile="已确认基线",
            tofile="拟变更正文",
            lineterm="",
        )
    )
    return {"base_revision": snapshot["project"]["revision"], "diff": difference[:16000]}

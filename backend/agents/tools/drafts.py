from backend.models.projects import DraftItem, ProjectError


async def prepare(context, arguments):
    draft = DraftItem.model_validate(arguments)
    if draft.kind in {"task", "acceptance"} and context.principal.account.role != "admin":
        raise ProjectError("FORBIDDEN", "内部任务与交付记录由负责人创建", 403)
    snapshot = await context.store.detail(context.principal, context.project_id)
    if draft.kind != "specification" and not snapshot["project"]["revision"]:
        raise ProjectError("BASELINE_REQUIRED", "请先完成初版需求书确认")
    if len(context.drafts) >= 3:
        raise ValueError("每轮最多准备三份草稿")
    context.drafts.append(draft.model_dump())
    return {
        "prepared": True,
        "title": draft.title,
        "kind": draft.kind,
        "requires_confirmation": True,
        "message": "草稿随本轮成功后保存，尚未提交或发布",
    }

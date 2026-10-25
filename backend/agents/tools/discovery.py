from backend.models.projects import DiscoveryUpdate


async def remember(context, arguments):
    notes = DiscoveryUpdate.model_validate(arguments).model_dump(exclude_unset=True)
    context.notes.append(notes)
    return {"prepared": True, "updated_fields": list(notes), "message": "沟通纪要随本轮成功后保存"}

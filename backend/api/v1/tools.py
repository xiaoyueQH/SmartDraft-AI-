from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.v1.auth import AdminDep, PrincipalDep
from backend.db.tool_store import ToolStore
from backend.dependencies import DatabaseDep
from backend.models.consultation import SetTool

router = APIRouter(tags=["agent-tools"])


def tool_store(database: DatabaseDep):
    return ToolStore(database)


StoreDep = Annotated[ToolStore, Depends(tool_store)]


@router.get("/agent/tools")
async def tools(principal: PrincipalDep, store: StoreDep):
    return {"web_search": await store.get(principal)}


@router.post("/admin/agent/tools/web-search")
async def update_tool(data: SetTool, principal: AdminDep, store: StoreDep):
    return await store.set(principal, data)

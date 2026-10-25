from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.api.v1.auth import PrincipalDep
from backend.db.project_store import ProjectStore
from backend.dependencies import DatabaseDep
from backend.models.projects import (
    CreateItem,
    ItemAction,
    ManageRecords,
    SendMessage,
    StartConsultation,
)

router = APIRouter(prefix="/projects", tags=["software-projects"])
consultations_router = APIRouter(prefix="/consultations", tags=["requirement-discovery"])


def project_store(database: DatabaseDep):
    return ProjectStore(database)


StoreDep = Annotated[ProjectStore, Depends(project_store)]


@router.get("")
async def projects(
    principal: PrincipalDep,
    store: StoreDep,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    deleted: bool = False,
    q: Annotated[str, Query(max_length=80)] = "",
):
    return await store.projects(principal, offset, limit, deleted=deleted, query=q)


@router.post("/records/actions")
async def manage_records(data: ManageRecords, principal: PrincipalDep, store: StoreDep):
    return await store.manage_records(principal, data)


@consultations_router.post("")
async def start_consultation(data: StartConsultation, principal: PrincipalDep, store: StoreDep):
    return await store.start_consultation(principal, data)


@router.get("/{project_id}")
async def project_detail(project_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.detail(principal, project_id)


@router.get("/{project_id}/activity")
async def activity(project_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.activity(principal, project_id)


@router.get("/{project_id}/items")
async def items(
    project_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
):
    return await store.items(principal, project_id, offset, limit)


@router.post("/{project_id}/items")
async def create_item(project_id: UUID, data: CreateItem, principal: PrincipalDep, store: StoreDep):
    return await store.create_item(principal, project_id, data)


@router.get("/{project_id}/items/{item_id}")
async def item(project_id: UUID, item_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.item(principal, project_id, item_id)


@router.get("/{project_id}/items/{item_id}/comparison")
async def comparison(
    project_id: UUID,
    item_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    expected_version: Annotated[int, Query(ge=1)],
):
    return await store.comparison(principal, project_id, item_id, expected_version)


@router.post("/{project_id}/items/{item_id}/actions")
async def item_action(
    project_id: UUID, item_id: UUID, data: ItemAction, principal: PrincipalDep, store: StoreDep
):
    return await store.act(principal, project_id, item_id, data)


@router.get("/{project_id}/agent")
async def agent_status(
    project_id: UUID, request: Request, principal: PrincipalDep, store: StoreDep
):
    await store.detail(principal, project_id)
    return {
        "configured": request.app.state.project_agent.configured,
        "model": request.app.state.settings.agent.model,
    }


@router.post("/{project_id}/messages", status_code=202)
async def message(
    project_id: UUID, data: SendMessage, request: Request, principal: PrincipalDep, store: StoreDep
):
    agent = request.app.state.project_agent
    agent.reserve()
    try:
        run, created = await store.start_run(principal, project_id, data)
    except BaseException:
        agent.release()
        raise
    if created:
        agent.launch(store, principal, project_id, UUID(run["id"]), data.content)
    else:
        agent.release()
    return run


@router.get("/{project_id}/runs/{run_id}")
async def run(project_id: UUID, run_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.run(principal, project_id, run_id)

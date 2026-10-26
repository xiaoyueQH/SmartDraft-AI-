import asyncio
import json
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.v1.auth import PrincipalDep
from backend.db.project_store import ProjectStore
from backend.db.tool_store import ToolStore
from backend.dependencies import CacheDep, DatabaseDep
from backend.models.auth import AuthError
from backend.models.projects import (
    CreateItem,
    ItemAction,
    ManageRecords,
    ProjectError,
    SendMessage,
    StartConsultation,
)

router = APIRouter(prefix="/projects", tags=["software-projects"])
consultations_router = APIRouter(prefix="/consultations", tags=["requirement-discovery"])


def project_store(database: DatabaseDep, cache: CacheDep):
    return ProjectStore(database, cache)


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
async def activity(
    project_id: UUID, principal: PrincipalDep, store: StoreDep, item_id: UUID | None = None
):
    return await store.activity(principal, project_id, item_id)


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
        "configured": request.app.state.agent_runtime.configured,
        "model": request.app.state.agent_runtime.models.profiles["consultation"].model,
        "models": {
            name: profile.model
            for name, profile in request.app.state.agent_runtime.models.profiles.items()
        },
        "tools": {"web_search": await ToolStore(store.database).get(principal)},
    }


@router.post("/{project_id}/messages", status_code=202)
async def message(
    project_id: UUID, data: SendMessage, request: Request, principal: PrincipalDep, store: StoreDep
):
    agent = request.app.state.agent_runtime
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


@router.get("/{project_id}/runs/{run_id}/stream")
async def stream_run(
    project_id: UUID, run_id: UUID, request: Request, principal: PrincipalDep, store: StoreDep
):
    initial = await store.run(principal, project_id, run_id)
    runtime = request.app.state.agent_runtime
    viewers = runtime.stream_viewers
    sid = str(principal.session_id)
    if sum(viewers.values()) >= 64 or viewers.get(sid, 0) >= 2:
        raise ProjectError("STREAM_BUSY", "实时连接繁忙，请稍后重试", 503)
    token = request.headers["authorization"].split(" ", 1)[1]

    async def events():
        # Reserve only once iteration begins, so disconnect-before-body cannot leak a viewer.
        if sum(viewers.values()) >= 64 or viewers.get(sid, 0) >= 2:
            yield ('event: error\ndata: {"message":"实时连接繁忙，请稍后重试","status":503}\n\n')
            return
        viewers[sid] = viewers.get(sid, 0) + 1
        current, checked, previous, started = initial, monotonic(), "", monotonic()
        try:
            while monotonic() - started < 25:
                if await request.is_disconnected():
                    return
                if monotonic() - checked >= 1:
                    # Recheck token expiry, revocation, role and project access on long connections.
                    actor = await request.app.state.auth_service.authenticate(token)
                    current = await store.run(actor, project_id, run_id)
                    checked = monotonic()
                preview = (
                    await runtime.preview(store, run_id) if current["status"] == "running" else None
                )
                payload = json.dumps({**current, "preview": preview}, ensure_ascii=False)
                if payload != previous:
                    yield f"event: run\ndata: {payload}\n\n"
                    previous = payload
                if current["status"] != "running":
                    return
                await asyncio.sleep(0.12)
        except (AuthError, ProjectError) as error:
            yield (
                "event: error\ndata: "
                + json.dumps(
                    {"message": error.message, "code": error.code, "status": error.status},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        except Exception:
            yield (
                'event: error\ndata: {"message":"实时连接暂时中断，正在重新连接","status":503}\n\n'
            )
        finally:
            viewers[sid] -= 1
            if not viewers[sid]:
                del viewers[sid]

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Auth-Session": sid,
        },
    )

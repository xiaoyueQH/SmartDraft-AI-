import asyncio
import json
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.v1.auth import PrincipalDep
from backend.db.conversation_store import ConversationStore
from backend.db.tool_store import ToolStore
from backend.dependencies import CacheDep, DatabaseDep, SettingsDep
from backend.models.auth import AuthError
from backend.models.projects import (
    ConversationAction,
    CreateItem,
    ItemAction,
    ManageRecords,
    ProjectError,
    SendMessage,
    StartConsultation,
    UndoDocument,
)

router = APIRouter(prefix="/projects", tags=["software-projects"])
consultations_router = APIRouter(prefix="/consultations", tags=["requirement-discovery"])


def project_store(database: DatabaseDep, cache: CacheDep, settings: SettingsDep):
    return ConversationStore(database, cache, settings.recovery, settings.queue)


StoreDep = Annotated[ConversationStore, Depends(project_store)]


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
    return await store.detail(principal, project_id, summaries=True)


@router.get("/{project_id}/activity")
async def activity(
    project_id: UUID, principal: PrincipalDep, store: StoreDep, item_id: UUID | None = None
):
    return await store.activity(principal, project_id, item_id)


@router.get("/{project_id}/fact-sources")
async def fact_sources(
    project_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await store.fact_sources(principal, project_id, offset, limit)


@router.get("/{project_id}/fact-sources/{evidence_id}")
async def fact_source(project_id: UUID, evidence_id: str, principal: PrincipalDep, store: StoreDep):
    return await store.fact_source(principal, project_id, evidence_id)


@router.get("/{project_id}/items")
async def items(
    project_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
):
    return await store.items(principal, project_id, offset, limit, summaries=True)


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


@router.get("/{project_id}/items/{item_id}/blocks")
async def document_blocks(
    project_id: UUID,
    item_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    expected_version: Annotated[int | None, Query(ge=1)] = None,
):
    return await store.document_index(principal, project_id, item_id, expected_version)


@router.get("/{project_id}/items/{item_id}/latest-change")
async def latest_document_change(
    project_id: UUID, item_id: UUID, principal: PrincipalDep, store: StoreDep
):
    return await store.latest_document_change(principal, project_id, item_id)


@router.post("/{project_id}/items/{item_id}/undo-change")
async def undo_document_change(
    project_id: UUID,
    item_id: UUID,
    data: UndoDocument,
    principal: PrincipalDep,
    store: StoreDep,
):
    return await store.undo_document_change(principal, project_id, item_id, data)


@router.post("/{project_id}/items/{item_id}/actions")
async def item_action(
    project_id: UUID, item_id: UUID, data: ItemAction, principal: PrincipalDep, store: StoreDep
):
    return await store.act(principal, project_id, item_id, data)


@router.get("/{project_id}/agent")
async def agent_status(
    project_id: UUID, request: Request, principal: PrincipalDep, store: StoreDep
):
    await store.authorize(principal, project_id)
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
    delivery = await store.delivery(principal, project_id, data)
    if "receipt" in delivery:
        return delivery["receipt"]
    if delivery["chat"]:
        result = await store.send_chat(principal, project_id, data)
        await agent.try_facts(store, principal, project_id)
        return result
    if not agent.configured:
        raise ProjectError("AGENT_NOT_CONFIGURED", "业务顾问暂时不可用，请联系负责人。", 503)
    run, _ = await store.start_run(
        principal, project_id, data, conversation_version=delivery["version"]
    )
    return run


@router.post("/{project_id}/conversation/actions")
async def conversation_action(
    project_id: UUID,
    data: ConversationAction,
    request: Request,
    principal: PrincipalDep,
    store: StoreDep,
):
    result = await store.action(principal, project_id, data)
    await request.app.state.agent_runtime.try_facts(store, principal, project_id)
    return result


@router.get("/{project_id}/conversation")
async def conversation(
    project_id: UUID, request: Request, principal: PrincipalDep, store: StoreDep
):
    result = await store.snapshot(principal, project_id)
    if result["conversation"]["mode"] == "human" and result["conversation"]["extraction_pending"]:
        await request.app.state.agent_runtime.try_facts(store, principal, project_id)
    return result


@router.get("/{project_id}/messages/history")
async def message_history(
    project_id: UUID,
    principal: PrincipalDep,
    store: StoreDep,
    before: Annotated[int | None, Query(ge=1)] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    q: Annotated[str, Query(max_length=120)] = "",
    sender: Annotated[str, Query(pattern="^(customer|support|ai|system|legacy)?$")] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
):
    return await store.history(
        principal, project_id, before=before, after=after, q=q, sender=sender, limit=limit
    )


@router.get("/{project_id}/messages/{message_id}/context")
async def message_context(
    project_id: UUID, message_id: UUID, principal: PrincipalDep, store: StoreDep
):
    return await store.context(principal, project_id, message_id)


@router.get("/{project_id}/conversation/stream")
async def conversation_stream(
    project_id: UUID, request: Request, principal: PrincipalDep, store: StoreDep
):
    await store.authorize(principal, project_id)
    runtime, sid = request.app.state.agent_runtime, str(principal.session_id)
    viewers = runtime.stream_viewers
    if sum(viewers.values()) >= 64 or viewers.get(sid, 0) >= 2:
        raise ProjectError("STREAM_BUSY", "实时连接繁忙，请稍后重试", 503)
    token = request.headers["authorization"].split(" ", 1)[1]

    async def events():
        if sum(viewers.values()) >= 64 or viewers.get(sid, 0) >= 2:
            yield 'event: error\ndata: {"message":"实时连接繁忙，请稍后重试","status":503}\n\n'
            return
        viewers[sid] = viewers.get(sid, 0) + 1
        previous, started = "", monotonic()
        try:
            while monotonic() - started < 25:
                if await request.is_disconnected():
                    return
                actor = await request.app.state.auth_service.authenticate(token)
                current = await store.snapshot(actor, project_id)
                if (
                    current["conversation"]["mode"] == "human"
                    and current["conversation"]["extraction_pending"]
                ):
                    await runtime.try_facts(store, actor, project_id)
                active = current.get("run")
                if active and active["status"] == "running":
                    active["preview"] = await runtime.preview(store, UUID(active["id"]))
                payload = json.dumps(current, ensure_ascii=False)
                if payload != previous:
                    yield f"event: conversation\ndata: {payload}\n\n"
                    previous = payload
                await asyncio.sleep(0.5)
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
            yield 'event: error\ndata: {"message":"实时连接暂时中断，正在重连","status":503}\n\n'
        finally:
            viewers[sid] = max(0, viewers.get(sid, 1) - 1)
            if not viewers[sid]:
                viewers.pop(sid, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Auth-Session": sid,
        },
    )


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

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.agents.review.graph import resume_review
from backend.api.v1.auth import PrincipalDep
from backend.db.review_store import ReviewStore
from backend.dependencies import CacheDep, DatabaseDep
from backend.models.review import ApplyReview, StartReview

router = APIRouter(prefix="/projects/{project_id}/items/{item_id}", tags=["document-review"])


def review_store(database: DatabaseDep, cache: CacheDep):
    return ReviewStore(database, cache)


StoreDep = Annotated[ReviewStore, Depends(review_store)]


@router.post("/reviews", status_code=202)
async def start_review(
    project_id: UUID,
    item_id: UUID,
    data: StartReview,
    request: Request,
    principal: PrincipalDep,
    store: StoreDep,
):
    runtime = request.app.state.agent_runtime
    runtime.reserve()
    try:
        result, created = await store.start(principal, project_id, item_id, data)
    except BaseException:
        runtime.release()
        raise
    if created:
        runtime.launch_review(store, principal, project_id, item_id, UUID(result["id"]))
    else:
        runtime.release()
    return result


@router.get("/reviews")
async def latest_review(project_id: UUID, item_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.latest(principal, project_id, item_id)


@router.get("/reviews/{review_id}")
async def review(
    project_id: UUID, item_id: UUID, review_id: UUID, principal: PrincipalDep, store: StoreDep
):
    return await store.load(principal, project_id, item_id, review_id)


@router.post("/reviews/{review_id}/decision")
async def decide(
    project_id: UUID,
    item_id: UUID,
    review_id: UUID,
    data: ApplyReview,
    request: Request,
    principal: PrincipalDep,
    store: StoreDep,
):
    return await resume_review(
        request.app.state.agent_runtime, store, principal, project_id, item_id, review_id, data
    )


@router.get("/history")
async def history(project_id: UUID, item_id: UUID, principal: PrincipalDep, store: StoreDep):
    return await store.history(principal, project_id, item_id)


@router.post("/history/{revision_id}/restore")
async def restore(
    project_id: UUID,
    item_id: UUID,
    revision_id: UUID,
    data: StartReview,
    principal: PrincipalDep,
    store: StoreDep,
):
    return await store.restore(principal, project_id, item_id, revision_id, data)

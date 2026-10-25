from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from backend.api.v1.auth import AdminDep
from backend.db.project_admin_store import ProjectAdminStore
from backend.dependencies import DatabaseDep

router = APIRouter(prefix="/admin/projects", tags=["project-management"])


def admin_store(database: DatabaseDep):
    return ProjectAdminStore(database)


StoreDep = Annotated[ProjectAdminStore, Depends(admin_store)]
Offset = Annotated[int, Query(ge=0, le=10000)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=80)]


@router.get("/summary")
async def summary(principal: AdminDep, store: StoreDep):
    return await store.summary(principal)


@router.get("")
async def projects(
    principal: AdminDep,
    store: StoreDep,
    offset: Offset = 0,
    limit: Limit = 20,
    q: Search = "",
    phase: Literal["", "discovery", "project"] = "",
):
    return await store.projects(principal, offset=offset, limit=limit, query=q, phase=phase)


@router.get("/items")
async def items(
    principal: AdminDep,
    store: StoreDep,
    category: Literal["documents", "tasks", "deliveries", "service"],
    offset: Offset = 0,
    limit: Limit = 20,
    q: Search = "",
    status: Literal[
        "active",
        "all",
        "pending_review",
        "draft",
        "submitted",
        "in_progress",
        "awaiting_acceptance",
        "approved",
        "closed",
        "rejected",
        "withdrawn",
    ] = "active",
):
    return await store.items(
        principal, category=category, offset=offset, limit=limit, query=q, status=status
    )

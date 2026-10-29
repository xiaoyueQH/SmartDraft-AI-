from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from backend.api.v1.auth import AdminDep, PrincipalDep
from backend.db.conversation_store import ConversationStore
from backend.dependencies import CacheDep, DatabaseDep, SettingsDep

router = APIRouter(prefix="/conversations", tags=["customer-conversations"])


def conversation_store(database: DatabaseDep, cache: CacheDep, settings: SettingsDep):
    return ConversationStore(database, cache, settings.recovery)


StoreDep = Annotated[ConversationStore, Depends(conversation_store)]


@router.get("/requests")
async def requests(
    principal: AdminDep,
    store: StoreDep,
    reason: Literal[
        "", "waiting", "handover", "unanswered", "human_only", "failed", "expiring"
    ] = "",
    scope: Literal["available", "all"] = "available",
    q: Annotated[str, Query(max_length=80)] = "",
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await store.requests(
        principal, reason=reason, scope=scope, q=q.strip(), offset=offset, limit=limit
    )


@router.get("/history")
async def history(
    principal: PrincipalDep,
    store: StoreDep,
    customer_id: UUID | None = None,
    project_id: UUID | None = None,
    q: Annotated[str, Query(max_length=120)] = "",
    sender: Annotated[str, Query(pattern="^(customer|support|ai|system|legacy)?$")] = "",
    cursor: Annotated[str, Query(max_length=160)] = "",
    start: AwareDatetime | None = None,
    end: AwareDatetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
):
    return await store.customer_history(
        principal,
        customer_id=customer_id,
        project_id=project_id,
        q=q,
        sender=sender,
        cursor=cursor,
        start=start,
        end=end,
        limit=limit,
    )

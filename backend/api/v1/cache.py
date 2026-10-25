from fastapi import APIRouter, Response

from backend.api.v1.auth import AdminDep
from backend.dependencies import CacheDep

router = APIRouter(prefix="/admin/cache", tags=["cache"])


@router.get("")
async def cache_status(principal: AdminDep, cache: CacheDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await cache.status()

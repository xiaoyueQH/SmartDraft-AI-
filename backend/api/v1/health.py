import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from backend.dependencies import DatabaseDep, SettingsDep

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger("supportflow.health")


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "supportflow"
    version: str = "0.1.0"


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "unavailable"]]


@router.get("/live", response_model=Liveness)
async def liveness(response: Response) -> Liveness:
    response.headers["Cache-Control"] = "no-store"
    return Liveness()


@router.get("/ready", response_model=Readiness, responses={503: {"model": Readiness}})
async def readiness(response: Response, database: DatabaseDep, settings: SettingsDep) -> Readiness:
    response.headers["Cache-Control"] = "no-store"
    try:
        async with asyncio.timeout(settings.database.health_timeout_seconds):
            await database.ping()
    except Exception as exc:
        logger.warning("database_not_ready error_type=%s", type(exc).__name__)
        response.status_code = 503
        return Readiness(status="not_ready", checks={"postgresql": "unavailable"})
    return Readiness(status="ready", checks={"postgresql": "ok"})

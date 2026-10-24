from fastapi import APIRouter

from backend.api.v1.auth import admin_router
from backend.api.v1.auth import router as auth_router
from backend.api.v1.health import router as health_router

router = APIRouter(prefix="/api")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(admin_router)

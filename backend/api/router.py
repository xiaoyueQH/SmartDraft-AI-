from fastapi import APIRouter

from backend.api.v1.auth import admin_router
from backend.api.v1.auth import router as auth_router
from backend.api.v1.health import router as health_router
from backend.api.v1.project_admin import router as project_admin_router
from backend.api.v1.projects import consultations_router
from backend.api.v1.projects import router as projects_router

router = APIRouter(prefix="/api")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(consultations_router)
router.include_router(projects_router)
router.include_router(project_admin_router)

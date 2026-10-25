from fastapi import APIRouter

from backend.api.v1.auth import admin_router
from backend.api.v1.auth import router as auth_router
from backend.api.v1.cancellations import admin_router as admin_cancellations_router
from backend.api.v1.cancellations import router as cancellations_router
from backend.api.v1.catalog import router as catalog_router
from backend.api.v1.checkout import router as checkout_router
from backend.api.v1.health import router as health_router
from backend.api.v1.orders import admin_router as admin_orders_router
from backend.api.v1.orders import router as orders_router
from backend.api.v1.workflow import router as workflow_router

router = APIRouter(prefix="/api")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(orders_router)
router.include_router(admin_orders_router)
router.include_router(cancellations_router)
router.include_router(admin_cancellations_router)
router.include_router(catalog_router)
router.include_router(checkout_router)
router.include_router(workflow_router)

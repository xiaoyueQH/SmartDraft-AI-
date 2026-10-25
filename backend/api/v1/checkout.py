from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.v1.auth import PrincipalDep
from backend.db.checkout_store import CheckoutStore
from backend.dependencies import DatabaseDep
from backend.models.checkout import CheckoutRequest, CheckoutResult
from backend.services.checkout import CheckoutService

router = APIRouter(tags=["checkout"])


def checkout_service(database: DatabaseDep) -> CheckoutService:
    return CheckoutService(CheckoutStore(database))


CheckoutDep = Annotated[CheckoutService, Depends(checkout_service)]


@router.post("/orders", response_model=CheckoutResult)
async def create_order(data: CheckoutRequest, principal: PrincipalDep, service: CheckoutDep):
    return await service.create(principal, data)

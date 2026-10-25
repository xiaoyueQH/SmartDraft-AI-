from backend.db.checkout_store import CheckoutStore
from backend.models.auth import Principal
from backend.models.checkout import CheckoutRequest, CheckoutResult


class CheckoutService:
    def __init__(self, store: CheckoutStore):
        self.store = store

    async def create(self, principal: Principal, data: CheckoutRequest) -> CheckoutResult:
        return await self.store.create(principal, data)

from uuid import UUID

from backend.db.cancellation_store import CancellationStore
from backend.models.auth import Principal
from backend.models.cancellation import CancellationCommit, CancellationReason
from backend.services.orders import OrderService


class CancellationService:
    def __init__(self, store: CancellationStore):
        self.store = store

    async def preview(
        self, principal: Principal, order_id: UUID, reason: str, *, all_users: bool = False
    ):
        OrderService.scope(principal, all_users)
        cleaned = CancellationReason(reason=reason).reason
        return await self.store.preview(principal, order_id, cleaned, all_users)

    async def confirm(
        self,
        principal: Principal,
        order_id: UUID,
        confirmation_id: UUID,
        confirmed: bool,
        *,
        all_users: bool = False,
    ):
        OrderService.scope(principal, all_users)
        CancellationCommit(confirmation_id=confirmation_id, confirmed=confirmed)
        return await self.store.confirm(principal, order_id, confirmation_id, all_users)

from uuid import UUID

from backend.db.order_store import OrderStore
from backend.models.auth import AuthError, Principal
from backend.models.orders import OrderDetail, OrderPage, OrderStatus


class OrderService:
    def __init__(self, store: OrderStore):
        self.store = store

    @staticmethod
    def scope(principal: Principal, all_users: bool) -> UUID | None:
        if all_users:
            if principal.account.role != "admin":
                raise AuthError("FORBIDDEN", "需要管理员权限", 403)
            return None
        return principal.account.id

    async def list_orders(
        self,
        principal: Principal,
        *,
        all_users: bool = False,
        status: OrderStatus | None = None,
        number: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> OrderPage:
        return await self.store.list_orders(
            self.scope(principal, all_users), status, number, limit, offset
        )

    async def detail(
        self, principal: Principal, order_id: UUID, *, all_users: bool = False
    ) -> OrderDetail:
        return await self.store.detail(order_id, self.scope(principal, all_users))

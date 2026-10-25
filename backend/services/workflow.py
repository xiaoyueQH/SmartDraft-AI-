from uuid import UUID

from backend.db.workflow_store import WorkflowStore
from backend.models.auth import AuthError, Principal
from backend.models.workflow import ADMIN_ACTIONS, Action, Confirmation, ReturnStatus


class WorkflowService:
    def __init__(self, store: WorkflowStore):
        self.store = store

    async def run(
        self, principal: Principal, order_id: UUID, action: Action, command: Confirmation
    ):
        if action in ADMIN_ACTIONS and principal.account.role != "admin":
            raise AuthError("FORBIDDEN", "需要管理员权限", 403)
        return await self.store.run(principal, order_id, action, command)

    async def returns(
        self,
        principal: Principal,
        status: ReturnStatus | None,
        limit: int,
        offset: int,
        *,
        admin: bool = False,
    ):
        if admin and principal.account.role != "admin":
            raise AuthError("FORBIDDEN", "需要管理员权限", 403)
        return await self.store.returns(
            None if admin else principal.account.id, status, limit, offset
        )

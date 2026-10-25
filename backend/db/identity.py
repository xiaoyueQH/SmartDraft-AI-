from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.auth_store import utc
from backend.models.auth import AuthError, Principal
from backend.models.orm import LoginSession, User


async def lock_identity(
    session: AsyncSession, principal: Principal, *, admin: bool = False, shared: bool = False
) -> User:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '1500ms'"))
    user = await session.scalar(
        select(User).where(User.id == principal.account.id).with_for_update(read=shared)
    )
    login = await session.scalar(
        select(LoginSession)
        .where(
            LoginSession.id == principal.session_id,
            LoginSession.user_id == principal.account.id,
        )
        .with_for_update(read=shared)
    )
    if (
        not user
        or not user.is_active
        or not login
        or login.revoked_at is not None
        or utc(login.expires_at) <= datetime.now(UTC)
        or user.password_hash != principal.account.password_hash
    ):
        raise AuthError("SESSION_INVALID", "登录已失效，请重新登录")
    if admin and user.role != "admin":
        raise AuthError("FORBIDDEN", "需要管理员权限", 403)
    return user

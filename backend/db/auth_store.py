import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from backend.db.session import Database
from backend.models.auth import Account, AuthError, Principal
from backend.models.orm import LoginBucket, LoginSession, User


def account(row: User) -> Account:
    return Account(
        row.id,
        row.username,
        row.display_name,
        row.role,
        row.is_active,
        row.password_hash,
        row.created_at,
    )


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class AuthStore:
    def __init__(self, database: Database):
        self.database = database

    async def check_login_budget(self, client: str, username: str, limit: int) -> None:
        now = datetime.now(UTC)
        keys = {
            hashlib.sha256(f"account:{username.strip().lower()}".encode()).hexdigest(): limit,
            hashlib.sha256(f"client:{client}".encode()).hexdigest(): limit * 10,
        }
        blocked = False
        insert = sqlite_insert if self.database.engine.dialect.name == "sqlite" else pg_insert
        expired = LoginBucket.window_started <= now - timedelta(seconds=60)
        async with self.database.session() as session:
            await session.execute(
                delete(LoginBucket).where(LoginBucket.window_started < now - timedelta(minutes=10))
            )
        async with self.database.session() as session:
            for key, allowed in sorted(keys.items()):
                statement = insert(LoginBucket).values(key=key, window_started=now, attempts=1)
                statement = statement.on_conflict_do_update(
                    index_elements=[LoginBucket.key],
                    set_={
                        "attempts": case((expired, 1), else_=LoginBucket.attempts + 1),
                        "window_started": case((expired, now), else_=LoginBucket.window_started),
                    },
                ).returning(LoginBucket.attempts)
                count = await session.scalar(statement)
                blocked = blocked or count > allowed
        if blocked:
            raise AuthError("LOGIN_LIMITED", "登录尝试过于频繁，请稍后重试", 429)

    async def find_user(self, username: str) -> Account | None:
        async with self.database.session() as session:
            row = await session.scalar(select(User).where(User.username == username))
            return account(row) if row else None

    async def create_user(
        self, username: str, display_name: str, role: str, encoded: str
    ) -> Account:
        try:
            async with self.database.session() as session:
                row = User(
                    username=username,
                    display_name=display_name,
                    role=role,
                    password_hash=encoded,
                    is_active=True,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                await session.flush()
                return account(row)
        except IntegrityError:
            raise AuthError("USERNAME_EXISTS", "该账号已存在", 409) from None

    async def list_users(self, limit: int, offset: int) -> list[Account]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
            )
            return [account(row) for row in rows]

    async def available_logins(self, proofs: dict[UUID, tuple[UUID, str]]) -> list[Principal]:
        if not proofs:
            return []
        now = datetime.now(UTC)
        async with self.database.session() as session:
            rows = await session.execute(
                select(User, LoginSession)
                .join(LoginSession, LoginSession.user_id == User.id)
                .where(
                    LoginSession.id.in_(proofs),
                    LoginSession.revoked_at.is_(None),
                    User.is_active.is_(True),
                    LoginSession.expires_at > now,
                    LoginSession.refresh_expires_at > now,
                )
            )
            return [
                Principal(account(user), login.id)
                for user, login in rows
                if proofs[login.id][0] == user.id
                and hmac.compare_digest(proofs[login.id][1], login.refresh_hash)
            ]

    async def create_login(
        self, user: Account, login: LoginSession, replaced: tuple[UUID, UUID] | None
    ) -> None:
        async with self.database.session() as session:
            current = await session.scalar(select(User).where(User.id == user.id).with_for_update())
            if not current or not current.is_active or current.password_hash != user.password_hash:
                raise AuthError("LOGIN_FAILED", "账号或密码不正确")
            if replaced:
                old_sid, old_uid = replaced
                await session.execute(
                    update(LoginSession)
                    .where(
                        LoginSession.id == old_sid,
                        LoginSession.user_id == old_uid,
                        LoginSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
            session.add(login)

    async def authenticate(self, user_id: UUID, session_id: UUID) -> Principal:
        async with self.database.session() as session:
            result = (
                await session.execute(
                    select(User, LoginSession)
                    .join(LoginSession, LoginSession.user_id == User.id)
                    .where(User.id == user_id, LoginSession.id == session_id)
                )
            ).first()
            if result:
                user, login = result
                if (
                    user.is_active
                    and login.revoked_at is None
                    and utc(login.expires_at) > datetime.now(UTC)
                ):
                    return Principal(account(user), login.id)
        raise AuthError("SESSION_INVALID", "登录已失效，请重新登录")

    async def rotate(
        self,
        user_id: UUID,
        session_id: UUID,
        presented_hash: str,
        next_hash: str,
        refresh_expires: datetime,
    ) -> tuple[Account, datetime]:
        invalid = False
        result = None
        now = datetime.now(UTC)
        async with self.database.session() as session:
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            login = await session.scalar(
                select(LoginSession)
                .where(LoginSession.id == session_id, LoginSession.user_id == user_id)
                .with_for_update()
            )
            if not user or not user.is_active or not login or login.revoked_at is not None:
                invalid = True
            elif utc(login.expires_at) <= now or utc(login.refresh_expires_at) <= now:
                login.revoked_at = now
                invalid = True
            elif not hmac.compare_digest(login.refresh_hash, presented_hash):
                login.revoked_at = now
                invalid = True
            else:
                expires = min(refresh_expires, utc(login.expires_at))
                login.refresh_hash = next_hash
                login.refresh_expires_at = expires
                result = (account(user), expires)
        if invalid or result is None:
            raise AuthError("SESSION_INVALID", "登录已失效，请重新登录")
        return result

    async def revoke(self, user_id: UUID, session_id: UUID) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(LoginSession)
                .where(
                    LoginSession.id == session_id,
                    LoginSession.user_id == user_id,
                    LoginSession.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )

    async def change_password(self, principal: Principal, encoded: str) -> None:
        async with self.database.session() as session:
            user = await session.scalar(
                select(User).where(User.id == principal.account.id).with_for_update()
            )
            login = await session.scalar(
                select(LoginSession)
                .where(
                    LoginSession.id == principal.session_id,
                    LoginSession.user_id == principal.account.id,
                )
                .with_for_update()
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
            user.password_hash = encoded
            await session.execute(
                update(LoginSession)
                .where(LoginSession.user_id == user.id, LoginSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )

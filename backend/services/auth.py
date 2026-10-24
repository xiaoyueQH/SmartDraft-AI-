import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.config import AuthSettings
from backend.core.security import JwtCodec, PasswordWork, token_hash
from backend.db.auth_store import AuthStore
from backend.models.auth import Account, AuthError, Principal
from backend.models.orm import LoginSession


@dataclass(frozen=True)
class TokenBundle:
    user: Account
    session_id: UUID
    refresh_expires: datetime
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


class AuthService:
    def __init__(
        self, store: AuthStore, codec: JwtCodec, passwords: PasswordWork, settings: AuthSettings
    ):
        self.store, self.codec, self.passwords, self.settings = store, codec, passwords, settings

    async def check_login_budget(self, client: str, username: str) -> None:
        await self.store.check_login_budget(
            client, username, self.settings.login_attempts_per_minute
        )

    async def create_user(
        self, username: str, display_name: str, role: str, password: str
    ) -> Account:
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]{2,31}", username):
            raise AuthError("USERNAME_INVALID", "账号需为 3～32 位字母、数字或 _ . -", 422)
        if role not in {"admin", "staff", "user"} or not 1 <= len(display_name.strip()) <= 64:
            raise AuthError("ACCOUNT_INVALID", "账号信息不符合要求", 422)
        self.validate_password(password)
        encoded = await self.passwords.hash(password)
        return await self.store.create_user(username, display_name.strip(), role, encoded)

    @staticmethod
    def validate_password(password: str) -> None:
        if not 12 <= len(password) <= 128 or len(password.encode()) > 512:
            raise AuthError("PASSWORD_INVALID", "密码需为 12～128 个字符", 422)

    def replaced_session(self, sid: UUID | None, refresh: str | None) -> tuple[UUID, UUID] | None:
        if sid and refresh:
            try:
                claims = self.codec.decode(refresh, "refresh", check_expiry=False)
                if UUID(claims["sid"]) == sid:
                    return sid, UUID(claims["sub"])
            except AuthError:
                pass
        return None

    async def login(
        self, username: str, password: str, replaced: tuple[UUID, UUID] | None = None
    ) -> TokenBundle:
        user = await self.store.find_user(username.strip().lower())
        valid = await self.passwords.verify(
            user.password_hash if user else self.passwords.dummy_hash, password
        )
        if not user or not valid or not user.is_active:
            raise AuthError("LOGIN_FAILED", "账号或密码不正确")
        now, sid, jti = datetime.now(UTC), uuid4(), str(uuid4())
        expires = now + timedelta(days=self.settings.session_days)
        refresh_expires = min(expires, now + timedelta(days=self.settings.refresh_idle_days))
        login = LoginSession(
            id=sid,
            user_id=user.id,
            refresh_hash=token_hash(jti),
            created_at=now,
            expires_at=expires,
            refresh_expires_at=refresh_expires,
        )
        await self.store.create_login(user, login, replaced)
        return self.bundle(user, sid, refresh_expires, jti)

    def bundle(self, user: Account, sid: UUID, refresh_expires: datetime, jti: str) -> TokenBundle:
        access_expires = min(
            refresh_expires, datetime.now(UTC) + timedelta(seconds=self.settings.access_seconds)
        )
        return TokenBundle(
            user,
            sid,
            refresh_expires,
            self.codec.issue("access", user.id, sid, access_expires),
            self.codec.issue("refresh", user.id, sid, refresh_expires, jti=jti),
        )

    async def refresh(self, sid: UUID, token: str) -> TokenBundle:
        claims = self.codec.decode(token, "refresh")
        if UUID(claims["sid"]) != sid:
            raise AuthError("TOKEN_INVALID", "登录凭据无效")
        jti = str(uuid4())
        user, expires = await self.store.rotate(
            UUID(claims["sub"]),
            sid,
            token_hash(claims["jti"]),
            token_hash(jti),
            datetime.now(UTC) + timedelta(days=self.settings.refresh_idle_days),
        )
        return self.bundle(user, sid, expires, jti)

    async def authenticate(self, token: str) -> Principal:
        claims = self.codec.decode(token, "access")
        return await self.store.authenticate(UUID(claims["sub"]), UUID(claims["sid"]))

    async def available_logins(self, cookies: list[tuple[UUID, str]]) -> list[Principal]:
        proofs = {}
        for sid, token in cookies[: self.settings.browser_session_limit]:
            try:
                claims = self.codec.decode(token, "refresh")
                if UUID(claims["sid"]) == sid:
                    proofs[sid] = (UUID(claims["sub"]), token_hash(claims["jti"]))
            except AuthError:
                continue
        return await self.store.available_logins(proofs)

    async def logout(self, sid: UUID, refresh: str | None, access: str | None) -> None:
        for token, purpose in ((refresh, "refresh"), (access, "access")):
            if not token:
                continue
            try:
                claims = self.codec.decode(token, purpose, check_expiry=False)
            except AuthError:
                continue
            if UUID(claims["sid"]) == sid:
                await self.store.revoke(UUID(claims["sub"]), sid)
                return

    async def change_password(self, principal: Principal, current: str, new: str) -> None:
        if not await self.passwords.verify(principal.account.password_hash, current):
            raise AuthError("PASSWORD_INCORRECT", "当前密码不正确", 400)
        self.validate_password(new)
        await self.store.change_password(principal, await self.passwords.hash(new))

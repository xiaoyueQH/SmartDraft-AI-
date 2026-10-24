import asyncio
import hashlib
import os
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from backend.config import AuthSettings
from backend.models.auth import AuthError


def load_signing_key(settings: AuthSettings) -> str:
    configured = settings.signing_key.get_secret_value()
    if configured:
        if len(configured.encode()) < 32:
            raise ValueError("JWT signing key must contain at least 32 bytes")
        return configured
    settings.key_file.parent.mkdir(parents=True, exist_ok=True)
    if not settings.key_file.exists():
        descriptor, temporary = tempfile.mkstemp(dir=settings.key_file.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(64))
            try:
                os.link(temporary, settings.key_file)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    key = settings.key_file.read_text(encoding="utf-8").strip()
    if len(key.encode()) < 32:
        raise ValueError("JWT key file is invalid")
    return key


def token_hash(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


class JwtCodec:
    def __init__(self, settings: AuthSettings, key: str):
        self.settings = settings
        self.key = key

    def issue(
        self,
        purpose: str,
        user_id: UUID,
        session_id: UUID,
        expires: datetime,
        *,
        jti: str | None = None,
    ) -> str:
        now = int(datetime.now(UTC).timestamp())
        return jwt.encode(
            {
                "iss": self.settings.issuer,
                "aud": f"supportflow.{purpose}",
                "sub": str(user_id),
                "sid": str(session_id),
                "typ": purpose,
                "jti": jti or str(uuid4()),
                "iat": now,
                "exp": int(expires.timestamp()),
            },
            self.key,
            algorithm="HS256",
        )

    def decode(self, token: str, purpose: str, *, check_expiry: bool = True) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self.key,
                algorithms=["HS256"],
                issuer=self.settings.issuer,
                audience=f"supportflow.{purpose}",
                options={
                    "require": ["iss", "aud", "sub", "sid", "typ", "jti", "iat", "exp"],
                    "verify_exp": check_expiry,
                    "strict_aud": True,
                },
            )
            if (
                claims["typ"] != purpose
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
            ):
                raise ValueError("Invalid claims")
            for field in ("sub", "sid", "jti"):
                UUID(claims[field])
            if claims["exp"] <= claims["iat"]:
                raise ValueError("Invalid lifetime")
            return claims
        except jwt.ExpiredSignatureError:
            raise AuthError(
                "ACCESS_EXPIRED" if purpose == "access" else "REFRESH_EXPIRED", "登录凭据已过期"
            ) from None
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, AttributeError):
            raise AuthError("TOKEN_INVALID", "登录凭据无效") from None


class PasswordWork:
    def __init__(self, capacity: int):
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
        self.slots = asyncio.Semaphore(capacity)
        self.pending: set[asyncio.Future] = set()
        self.dummy_hash = ""

    async def run(self, operation: Callable, *args):
        try:
            async with asyncio.timeout(0.5):
                await self.slots.acquire()
        except TimeoutError:
            raise AuthError("AUTH_BUSY", "登录服务繁忙，请稍后重试", 429) from None
        try:
            future = asyncio.get_running_loop().run_in_executor(None, operation, *args)
        except BaseException:
            self.slots.release()
            raise
        self.pending.add(future)

        def completed(done):
            self.pending.discard(done)
            self.slots.release()
            if not done.cancelled():
                done.exception()

        future.add_done_callback(completed)
        return await asyncio.shield(future)

    async def initialize(self) -> None:
        self.dummy_hash = await self.hash(secrets.token_urlsafe(32))

    async def hash(self, password: str) -> str:
        return await self.run(self.hasher.hash, password)

    async def verify(self, encoded: str, password: str) -> bool:
        def check():
            try:
                return self.hasher.verify(encoded, password)
            except (VerificationError, InvalidHashError):
                return False

        return await self.run(check)

    async def close(self) -> None:
        if self.pending:
            await asyncio.gather(
                *(asyncio.shield(item) for item in tuple(self.pending)), return_exceptions=True
            )

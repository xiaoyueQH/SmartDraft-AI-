import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from pydantic import SecretStr

from backend.config import AuthSettings
from backend.core.security import JwtCodec, PasswordWork, load_signing_key
from backend.models.auth import AuthError

KEY = "unit-test-key-" * 4


def test_signing_key_creation_is_atomic_across_workers(tmp_path):
    settings = AuthSettings(key_file=tmp_path / "jwt.key")
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: load_signing_key(settings), range(16)))
    assert len(set(keys)) == 1
    assert len(keys[0]) >= 32
    assert list(tmp_path.iterdir()) == [settings.key_file]


def test_tokens_have_distinct_purposes_and_required_claims():
    codec = JwtCodec(AuthSettings(signing_key=SecretStr(KEY)), KEY)
    access = codec.issue("access", uuid4(), uuid4(), datetime.now(UTC) + timedelta(minutes=5))
    assert codec.decode(access, "access")["typ"] == "access"
    with pytest.raises(AuthError):
        codec.decode(access, "refresh")
    claims = jwt.decode(access, KEY, algorithms=["HS256"], options={"verify_aud": False})
    for field in ("sid", "jti", "exp", "iat", "sub", "iss", "aud", "typ"):
        damaged = {key: value for key, value in claims.items() if key != field}
        with pytest.raises(AuthError):
            codec.decode(jwt.encode(damaged, KEY, algorithm="HS256"), "access")
    with pytest.raises(AuthError):
        codec.decode(jwt.encode(claims, "other-key-" * 5, algorithm="HS256"), "access")


async def test_password_hashing_and_cancellation_keep_worker_budget():
    work = PasswordWork(1)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(3)
        return "done"

    task = asyncio.create_task(work.run(slow))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        with pytest.raises(AuthError) as busy:
            await work.hash("Another-test-password")
        assert busy.value.status == 429
    finally:
        release.set()
        await work.close()
    encoded = await work.hash("Secure-test-password")
    assert await work.verify(encoded, "Secure-test-password")
    assert not await work.verify(encoded, "Wrong-test-password")
    await work.close()

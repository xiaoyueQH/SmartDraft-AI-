import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import get_settings  # noqa: E402
from backend.core.security import PasswordWork  # noqa: E402
from backend.db.auth_store import AuthStore  # noqa: E402
from backend.db.session import create_database  # noqa: E402
from backend.models.auth import AuthError  # noqa: E402

DEMO_ACCOUNTS = (("admin", "管理员", "admin"), ("user", "演示用户", "user"))
DEMO_PASSWORD = "123456"


async def seed() -> None:
    settings = get_settings()
    database = create_database(settings.database)
    passwords = PasswordWork(settings.auth.password_workers)
    store = AuthStore(database)
    try:
        print(
            f"目标数据库：{settings.database.host}:{settings.database.port}"
            f"/{settings.database.name}，账号表：public.users"
        )
        for username, display_name, role in DEMO_ACCOUNTS:
            existing = await store.find_user(username)
            if existing:
                print(f"已存在，保留原密码和角色：{username}（{existing.role}）")
                continue
            encoded = await passwords.hash(DEMO_PASSWORD)
            try:
                await store.create_user(username, display_name, role, encoded)
            except AuthError as exc:
                if exc.code != "USERNAME_EXISTS":
                    raise
                print(f"已存在，保留原密码和角色：{username}")
            else:
                print(f"已创建演示账号：{username}（{role}）")
    finally:
        await passwords.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(seed())

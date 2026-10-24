import argparse
import asyncio
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import get_settings  # noqa: E402
from backend.core.security import JwtCodec, PasswordWork, load_signing_key  # noqa: E402
from backend.db.auth_store import AuthStore  # noqa: E402
from backend.db.session import create_database  # noqa: E402
from backend.models.auth import AuthError  # noqa: E402
from backend.services.auth import AuthService  # noqa: E402


async def create(args, password):
    settings = get_settings()
    database = create_database(settings.database)
    passwords = PasswordWork(settings.auth.password_workers)
    try:
        service = AuthService(
            AuthStore(database),
            JwtCodec(settings.auth, load_signing_key(settings.auth)),
            passwords,
            settings.auth,
        )
        user = await service.create_user(
            args.username, args.display_name or args.username, args.role, password
        )
        print(f"已创建账号：{user.username}，角色：{user.role}")
    finally:
        await passwords.close()
        await database.close()


def main():
    parser = argparse.ArgumentParser(description="创建本地账号，密码只通过隐藏输入读取")
    parser.add_argument("username")
    parser.add_argument("--role", choices=["admin", "staff", "user"], default="user")
    parser.add_argument("--display-name")
    args = parser.parse_args()
    password = getpass.getpass("密码（至少 12 个字符）：")
    if password != getpass.getpass("再次输入密码："):
        raise SystemExit("两次密码不一致")
    try:
        asyncio.run(create(args, password))
    except AuthError as exc:
        raise SystemExit(exc.message) from None


if __name__ == "__main__":
    main()

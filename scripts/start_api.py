import logging
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import get_settings  # noqa: E402


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise SystemExit(f"配置校验失败，请检查字段：{fields}") from None
    logging.basicConfig(
        level=settings.app.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "backend.main:create_app",
        factory=True,
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.reload,
        reload_dirs=[str(PROJECT_ROOT / "backend")] if settings.app.reload else None,
        log_level=settings.app.log_level.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=settings.app.trusted_proxy_ips,
    )


if __name__ == "__main__":
    main()

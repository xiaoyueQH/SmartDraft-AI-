import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


if __name__ == "__main__":
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")

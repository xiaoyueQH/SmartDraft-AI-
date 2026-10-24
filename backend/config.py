from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AppSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    reload: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    request_capacity: int = Field(default=64, ge=1, le=2048)
    request_wait_seconds: float = Field(default=0.25, gt=0, le=5)
    trusted_proxy_ips: str = "127.0.0.1"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=15432, ge=1, le=65535)
    name: str = "supportflow"
    user: str = "supportflow"
    password: SecretStr = SecretStr("")
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=3, gt=0, le=60)
    connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    query_timeout_seconds: float = Field(default=5, gt=0, le=120)
    health_timeout_seconds: float = Field(default=2, gt=0, le=10)

    def connection_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


class AuthSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signing_key: SecretStr = SecretStr("")
    key_file: Path = PROJECT_ROOT / "storage/auth/jwt.key"
    issuer: str = "supportflow"
    access_seconds: int = Field(default=600, ge=30, le=1800)
    refresh_idle_days: int = Field(default=7, ge=1, le=30)
    session_days: int = Field(default=30, ge=1, le=90)
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5183",
        "http://localhost:5183",
        "http://127.0.0.1:8091",
        "http://localhost:8091",
    )
    browser_session_limit: int = Field(default=8, ge=1, le=16)
    password_workers: int = Field(default=4, ge=1, le=16)
    login_attempts_per_minute: int = Field(default=20, ge=1, le=100)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SF_",
        env_nested_delimiter="__",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        frozen=True,
        extra="forbid",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

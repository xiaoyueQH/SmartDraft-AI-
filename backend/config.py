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


class CacheSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=16379, ge=1, le=65535)
    database: int = Field(default=0, ge=0, le=15)
    password: SecretStr = SecretStr("")
    prefix: str = Field(default="supportflow:v1", pattern=r"^[a-zA-Z0-9:_-]{1,80}$")
    max_connections: int = Field(default=16, ge=1, le=100)
    timeout_seconds: float = Field(default=0.15, gt=0, le=1)
    cooldown_seconds: float = Field(default=5, ge=1, le=60)
    load_capacity: int = Field(default=8, ge=1, le=64)
    load_wait_seconds: float = Field(default=0.25, gt=0, le=2)
    run_ttl_seconds: int = Field(default=3, ge=1, le=5)
    summary_ttl_seconds: int = Field(default=15, ge=1, le=30)


class AgentSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1", pattern=r"^https?://[^\s\"']+$"
    )
    api_key: SecretStr = SecretStr("")
    model: str = "qwen-max"
    capacity: int = Field(default=4, ge=1, le=16)
    timeout_seconds: float = Field(default=30, ge=1, le=45)
    run_seconds: float = Field(default=90, ge=5, le=120)
    max_rounds: int = Field(default=5, ge=2, le=8)
    max_tools: int = Field(default=8, ge=1, le=12)
    token_budget: int = Field(default=40000, ge=4000, le=100000)
    search_timeout_seconds: float = Field(default=6, ge=1, le=15)
    draft_max_rounds: int = Field(default=8, ge=4, le=40)
    draft_max_tools: int = Field(default=8, ge=2, le=32)
    draft_token_budget: int = Field(default=64000, ge=16000, le=400000)
    draft_output_tokens: int = Field(default=6000, ge=2000, le=16000)
    draft_section_output_tokens: int = Field(default=2400, ge=1000, le=6000)
    draft_run_seconds: float = Field(default=150, ge=30, le=150)
    draft_model_timeout_seconds: float = Field(default=60, ge=5, le=90)
    draft_history_chars: int = Field(default=200000, ge=12000, le=500000)


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
    agent: AgentSettings = Field(default_factory=AgentSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

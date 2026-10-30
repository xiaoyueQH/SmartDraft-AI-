from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from backend.core.llm.models import ModelProfile, ModelPurpose

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
    consultation: ModelProfile = Field(default_factory=ModelProfile)
    draft_planner: ModelProfile = Field(default_factory=ModelProfile)
    draft_writer: ModelProfile = Field(default_factory=ModelProfile)
    draft_reviewer: ModelProfile = Field(default_factory=lambda: ModelProfile(temperature=0))
    fallbacks: dict[ModelPurpose, ModelProfile] = Field(default_factory=dict)
    review_max_rounds: int = Field(default=8, ge=2, le=12)
    review_max_tools: int = Field(default=16, ge=4, le=32)
    review_token_budget: int = Field(default=100000, ge=16000, le=200000)
    review_run_seconds: float = Field(default=120, ge=15, le=150)
    capacity: int = Field(default=4, ge=1, le=16)
    checkpoint_success_days: int = Field(default=1, ge=1, le=30)
    checkpoint_failure_days: int = Field(default=7, ge=1, le=90)
    checkpoint_cleanup_limit: int = Field(default=25, ge=1, le=100)
    timeout_seconds: float = Field(default=30, ge=1, le=45)
    run_seconds: float = Field(default=90, ge=5, le=120)
    max_rounds: int = Field(default=5, ge=2, le=8)
    max_tools: int = Field(default=8, ge=1, le=12)
    token_budget: int = Field(default=40000, ge=4000, le=100000)
    search_timeout_seconds: float = Field(default=6, ge=1, le=15)
    draft_max_rounds: int = Field(default=12, ge=4, le=40)
    draft_max_tools: int = Field(default=8, ge=2, le=32)
    draft_token_budget: int = Field(default=64000, ge=16000, le=400000)
    draft_output_tokens: int = Field(default=6000, ge=2000, le=16000)
    draft_section_output_tokens: int = Field(default=4000, ge=1000, le=6000)
    draft_run_seconds: float = Field(default=150, ge=30, le=150)
    draft_model_timeout_seconds: float = Field(default=60, ge=5, le=90)
    draft_history_chars: int = Field(default=200000, ge=12000, le=500000)


class QueueSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    capacity: int = Field(default=100, ge=1, le=10000)
    per_user: int = Field(default=4, ge=1, le=100)
    wait_seconds: int = Field(default=600, ge=30, le=3600)
    poll_seconds: float = Field(default=0.5, ge=0.01, le=10)
    lease_seconds: int = Field(default=30, ge=10, le=120)
    heartbeat_seconds: float = Field(default=5, ge=0.1, le=30)
    shutdown_seconds: float = Field(default=165, ge=1, le=300)

    @model_validator(mode="after")
    def heartbeat_before_expiry(self):
        if self.heartbeat_seconds * 3 >= self.lease_seconds:
            raise ValueError("任务心跳间隔必须小于租约期限的三分之一")
        return self


class RecoverySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    scan_seconds: float = Field(default=15, ge=1, le=60)
    batch_size: int = Field(default=40, ge=1, le=100)
    support_wait_seconds: int = Field(default=180, ge=30, le=3600)
    support_reply_warn_seconds: int = Field(default=120, ge=15, le=1800)
    support_reply_seconds: int = Field(default=300, ge=60, le=7200)
    support_idle_seconds: int = Field(default=900, ge=120, le=86400)
    support_notice_seconds: int = Field(default=60, ge=10, le=300)
    support_extend_seconds: int = Field(default=300, ge=60, le=1800)
    stalled_run_seconds: int = Field(default=180, ge=180, le=600)
    business_remind_seconds: int = Field(default=86400, ge=300, le=604800)

    @model_validator(mode="after")
    def ordered_deadlines(self):
        if self.support_reply_warn_seconds >= self.support_reply_seconds:
            raise ValueError("客服回复提醒时间必须早于恢复AI时间")
        if self.support_notice_seconds >= self.support_idle_seconds:
            raise ValueError("接待到期预告必须短于无互动期限")
        return self


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
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

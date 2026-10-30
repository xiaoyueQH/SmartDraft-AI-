"""Durable execution references; business payloads stay in their authoritative tables."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base


class AgentJob(Base):
    __tablename__ = "agent_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('run','review')", name="ck_agent_job_kind"),
        CheckConstraint(
            "status IN ('queued','running','completed','failed')", name="ck_agent_job_status"
        ),
        Index("uq_agent_job_target", "kind", "target_id", unique=True),
        Index("ix_agent_job_claim", "status", "created_at", "id"),
        Index("ix_agent_job_lease", "status", "lease_until"),
        Index("ix_agent_job_actor", "actor_id", "status"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(12))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[UUID] = mapped_column(Uuid)
    item_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(12), default="queued")
    token: Mapped[UUID | None] = mapped_column(Uuid)
    worker_id: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queued_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentWorker(Base):
    __tablename__ = "agent_workers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepting: Mapped[bool] = mapped_column(Boolean, default=True)

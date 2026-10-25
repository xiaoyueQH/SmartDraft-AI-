from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base


class Project(Base):
    __tablename__ = "delivery_projects"
    __table_args__ = (
        CheckConstraint("phase IN ('discovery','project')", name="ck_project_phase"),
        Index("ix_delivery_projects_deleted_created", "deleted_at", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    brief: Mapped[str] = mapped_column(Text)
    stack: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(16), default="discovery")
    discovery: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectItem(Base):
    __tablename__ = "project_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('specification','change','bug','support','task','acceptance')",
            name="ck_project_item_kind",
        ),
        Index("ix_project_items_project_created", "project_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    base_revision: Mapped[int] = mapped_column(Integer, default=0)
    published_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_confirmed: Mapped[bool] = mapped_column(default=False)
    staff_confirmed: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    resolution: Mapped[str] = mapped_column(Text, default="")
    acceptance_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectMessage(Base):
    __tablename__ = "project_messages"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectRecordEvent(Base):
    __tablename__ = "project_record_events"
    __table_args__ = (Index("ix_record_events_project_created", "project_id", "created_at"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(24))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectOperation(Base):
    __tablename__ = "project_operations"
    __table_args__ = (
        Index("uq_project_operations_actor_request", "actor_id", "request_id", unique=True),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectRun(Base):
    __tablename__ = "project_runs"
    __table_args__ = (
        Index("uq_project_run_request", "actor_id", "request_id", unique=True),
        Index(
            "uq_project_running",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")
    events: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Kind = Literal["specification", "change", "bug", "support", "task", "acceptance"]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID


class StartConsultation(Command):
    owner_id: UUID | None = None


class RecordTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_version: int = Field(ge=1)


class ManageRecords(Command):
    action: Literal["rename", "trash", "restore"]
    targets: list[RecordTarget] = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=120)


class DiscoveryNotes(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(default="", max_length=120)
    business_background: str = Field(default="", max_length=2000)
    current_workflow: str = Field(default="", max_length=2000)
    problems: list[str] = Field(default_factory=list, max_length=15)
    users: list[str] = Field(default_factory=list, max_length=15)
    goals: list[str] = Field(default_factory=list, max_length=15)
    scope: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=15)
    open_questions: list[str] = Field(default_factory=list, max_length=15)
    technology_preference: Literal["Python", "Java", "待确认"] = "待确认"


class DiscoveryUpdate(DiscoveryNotes):
    resolved_questions: list[str] = Field(default_factory=list, max_length=15)
    remove_facts: dict[Literal["problems", "users", "goals", "scope", "constraints"], list[str]] = (
        Field(default_factory=dict, max_length=5)
    )


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Kind
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=24000)
    questions: list[str] = Field(default_factory=list, max_length=20)


class CreateItem(Command, DraftItem):
    base_revision: int = Field(ge=0)


class ItemAction(Command):
    expected_version: int = Field(ge=1)
    action: Literal[
        "edit",
        "submit",
        "confirm",
        "reject",
        "start",
        "deliver",
        "accept",
        "reopen",
        "withdraw",
        "supplement",
    ]
    title: str = Field(default="", max_length=160)
    content: str = Field(default="", max_length=24000)
    questions: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=3000)


class SendMessage(Command):
    content: str = Field(min_length=1, max_length=5000)


class ProjectError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

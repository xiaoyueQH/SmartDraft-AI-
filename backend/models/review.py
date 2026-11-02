from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base
from backend.models.projects import Command


class DocumentReview(Base):
    __tablename__ = "document_reviews"
    __table_args__ = (
        Index("uq_document_review_request", "actor_id", "request_id", unique=True),
        Index(
            "uq_document_review_running",
            "item_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
        Index("ix_document_review_item_created", "item_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("project_items.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    source: Mapped[dict] = mapped_column(JSON)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate: Mapped[str] = mapped_column(Text, default="")
    events: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentRevision(Base):
    __tablename__ = "document_revisions"
    __table_args__ = (Index("uq_document_revision_version", "item_id", "version", unique=True),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("project_items.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    questions: Mapped[list] = mapped_column(JSON)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    action: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StartReview(Command):
    expected_version: int = Field(ge=1)


class ApplyReview(Command):
    expected_version: int = Field(ge=1)
    decision: Literal["apply", "discard"] = "apply"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=40)
    kind: Literal["technical", "client_mismatch", "decision", "suggestion"]
    severity: Literal["major", "minor"]
    block_ids: list[str] = Field(min_length=1, max_length=6)
    evidence: str = Field(
        min_length=1,
        max_length=3000,
        description="某一个目标块里的连续原文；不能拼接多个块、补代码围栏或省略中间行。",
    )
    explanation: str = Field(min_length=1, max_length=1500)
    recommendation: str = Field(min_length=1, max_length=1500)
    source_quote: str = Field(default="", max_length=2000)
    question: str = Field(default="", max_length=400)


class FindingsInput(BaseModel):
    findings: list[ReviewFinding] = Field(max_length=20)


class BlockChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    block_id: str
    old_text: str = Field(min_length=1, max_length=6000)
    new_text: str = Field(max_length=12000)
    finding_ids: list[str] = Field(min_length=1, max_length=6)


class ChangesInput(BaseModel):
    changes: list[BlockChange] = Field(min_length=1, max_length=12)


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    unresolved_ids: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=800)

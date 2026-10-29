import unicodedata
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    Boolean,
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
        CheckConstraint(
            "conversation_mode IN ('ai','pending','human')", name="ck_conversation_mode"
        ),
        Index("ix_delivery_projects_deleted_created", "deleted_at", "created_at"),
        Index("ix_project_support_due", "conversation_mode", "support_wait_since"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    brief: Mapped[str] = mapped_column(Text)
    stack: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(16), default="discovery")
    discovery: Mapped[dict] = mapped_column(JSON, default=dict)
    consultation: Mapped[dict] = mapped_column(JSON, default=dict)
    conversation_mode: Mapped[str] = mapped_column(String(16), default="ai")
    support_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    conversation_version: Mapped[int] = mapped_column(Integer, default=1)
    message_seq: Mapped[int] = mapped_column(Integer, default=0)
    facts_cursor: Mapped[int] = mapped_column(Integer, default=0)
    extraction_until: Mapped[int] = mapped_column(Integer, default=0)
    extraction_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_error: Mapped[str] = mapped_column(String(300), default="")
    support_wait_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_extended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_notice: Mapped[str] = mapped_column(String(24), default="")
    human_only: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_through: Mapped[int] = mapped_column(Integer, default=0)
    auto_reply_message_id: Mapped[UUID | None] = mapped_column(Uuid)
    auto_reply_request_id: Mapped[UUID | None] = mapped_column(Uuid)
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
        Index(
            "uq_current_requirement",
            "project_id",
            unique=True,
            postgresql_where=text("kind = 'specification' AND published_revision IS NULL"),
            sqlite_where=text("kind = 'specification' AND published_revision IS NULL"),
        ),
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
    workflow_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectMessage(Base):
    __tablename__ = "project_messages"
    __table_args__ = (
        Index("uq_project_message_seq", "project_id", "sequence", unique=True),
        CheckConstraint(
            "sender_type IN ('customer','support','ai','system','legacy')",
            name="ck_message_sender_type",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("project_runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sender_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sender_type: Mapped[str] = mapped_column(String(16), default="legacy")
    sender_name: Mapped[str] = mapped_column(String(100), default="")
    login_session_id: Mapped[UUID | None] = mapped_column(Uuid)
    reply_to_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
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
    action: Mapped[str] = mapped_column(String(64))
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
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="running")
    events: Mapped[list] = mapped_column(JSON, default=list)
    cache_version: Mapped[int] = mapped_column(Integer, default=1)
    answer: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Kind = Literal["specification", "change", "bug", "support", "task", "acceptance"]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID


class StartConsultation(Command):
    owner_id: UUID | None = None


class ConversationAction(Command):
    action: Literal[
        "request_human",
        "takeover",
        "release",
        "transfer",
        "retry_extraction",
        "extend",
        "resume_ai",
        "wait_human",
    ]
    expected_version: int = Field(ge=1)
    support_id: UUID | None = None


def human_help_requested(text):
    import re

    for clause in re.split(r"[，,。；;！!？?\n]", text):
        clause = clause.strip()
        if re.search(
            r"不要|不用|不需要|先别|如果|比如|例如|他说|客户说|怎么|如何|什么意思", clause
        ):
            continue
        if re.fullmatch(
            r"(?:人工|人工客服|真人客服|找人工)(?:吧|帮忙|谢谢)?|"
            r"(?:请|麻烦)?(?:帮我|给我)?(?:转|转接|联系|呼叫)(?:一下|到)?(?:人工|人工客服|真人客服)(?:吧|谢谢|帮忙)?"
            r"|(?:我想|我要|我需要|请|麻烦)(?:找|联系|呼叫|安排|转接|转|和)?(?:一位|一个)?(?:人工|人工客服|真人客服|客服)(?:帮忙|帮助|协助|聊聊|处理|接待|一下|吧|谢谢)*",
            clause,
        ):
            return True
        if re.fullmatch(
            r"(?:请|麻烦)?(?:帮我|给我)?(?:转|转接|联系|呼叫)(?:一下|到)?"
            r"(?:人工客服|真人客服|人工)(?:来)?(?:帮我|帮助我|协助我|帮忙|处理).+"
            r"|(?:我想|我要|我需要)(?:找|联系|呼叫|安排|转接|和)?(?:一位|一个)?"
            r"(?:人工客服|真人客服|人工|客服)(?:来)?(?:帮我|帮助我|协助我|帮忙|处理).+"
            r"|(?:请问)?(?:能不能|可以|能否)(?:帮我|给我)?(?:转|转接|联系)"
            r"(?:一下|到)?(?:人工客服|真人客服|人工)(?:吗|么|一下吗)?",
            clause,
        ):
            return True
    return False


def human_only_requested(text):
    import re

    return any(
        re.fullmatch(
            r"(?:请|我|现在|这次|本次|先|暂时|麻烦)?\s*(?:"
            r"只(?:要|等|找|接受|想和|想跟)(?:真人|人工)(?:客服)?(?:沟通|聊天|接待|帮助|协助|处理)?|"
            r"(?:不要|不用|不接受|别用|拒绝)(?:再|让)?\s*(?:AI|人工智能|机器人)(?:回复|回答|接待)?"
            r")(?:了|吧|谢谢)?",
            part.strip(),
            re.I,
        )
        for part in re.split(r"[，,。；;！!？?\n]", text)
    )


def human_control_only(text):
    import re

    parts = [part.strip() for part in re.split(r"[，,。；;！!？?\n]", text) if part.strip()]
    return bool(parts) and all(
        human_help_requested(part)
        or human_only_requested(part)
        or part in {"谢谢", "好的", "请", "麻烦了"}
        for part in parts
    )


class RecordTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    expected_version: int = Field(ge=1)


class ManageRecords(Command):
    action: Literal["rename", "trash", "restore"]
    targets: list[RecordTarget] = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def visible_name(self):
        if self.action == "rename" and not any(
            unicodedata.category(char)[0] in {"L", "N", "P", "S"} for char in self.title
        ):
            raise ValueError("记录名称需要包含可见文字或符号")
        return self


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
    technology_preference: str = Field(default="待确认", max_length=200)


class DiscoveryUpdate(DiscoveryNotes):
    resolved_questions: list[str] = Field(default_factory=list, max_length=15)
    remove_facts: dict[
        Literal["problems", "users", "goals", "scope", "constraints", "technology_preference"],
        list[str],
    ] = Field(default_factory=dict, max_length=5)


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Kind
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=120000)
    questions: list[str] = Field(default_factory=list, max_length=100)


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
    content: str = Field(default="", max_length=120000)
    questions: list[str] = Field(default_factory=list, max_length=100)
    note: str = Field(default="", max_length=3000)


class SendMessage(Command):
    content: str = Field(min_length=1, max_length=5000)
    intent: Literal["chat", "draft"] = Field(
        default="chat", description="chat提交新业务信息；draft仅根据已保存信息整理草案"
    )
    web_search: bool = False
    accepted_suggestions: list[str] = Field(default_factory=list, max_length=10)
    rejected_suggestions: list[str] = Field(default_factory=list, max_length=10)
    approved_search_query: str = Field(default="", max_length=120)
    decline_search: bool = False


class ProjectError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

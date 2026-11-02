from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, DateTime, ForeignKey, Index, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.drafting import DraftAssumption, DraftDecision
from backend.models.orm import Base
from backend.models.projects import Command, DiscoveryUpdate


class ToolSetting(Base):
    __tablename__ = "agent_tool_settings"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=1)


class ToolChange(Base):
    __tablename__ = "agent_tool_changes"
    __table_args__ = (Index("uq_tool_change_request", "actor_id", "request_id", unique=True),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    enabled: Mapped[bool]
    expected_version: Mapped[int]
    version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SetTool(Command):
    enabled: bool
    expected_version: int = Field(ge=0)


class GraphCheckpoint(Base):
    __tablename__ = "consultation_checkpoints"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(100), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(80))
    encoding: Mapped[str] = mapped_column(String(40))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    metadata_encoding: Mapped[str] = mapped_column(String(40))
    metadata_payload: Mapped[bytes] = mapped_column(LargeBinary)


class GraphWrite(Base):
    __tablename__ = "consultation_checkpoint_writes"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(100), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(100))
    encoding: Mapped[str] = mapped_column(String(40))
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class Gap(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=300)
    critical: bool = True


class FactSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal[
        "subject",
        "business_background",
        "current_workflow",
        "problems",
        "users",
        "goals",
        "scope",
        "constraints",
        "technology_preference",
        "resolved_questions",
    ]
    value: str = Field(
        min_length=1,
        max_length=2000,
        description="只填有实际内容的事实；未说明或未选择的字段不要创建空事实。",
    )
    intent: Literal["stated", "preferred", "required", "excluded", "unspecified"] = Field(
        default="stated",
        description="原话意图：陈述事实/偏好/必须/排除/未选定。不懂、举例和请顾问推荐属于unspecified，不是技术限制。",
    )
    quote: str = Field(
        default="",
        max_length=1000,
        description="直接复制本轮输入中的连续原文；不改字、不改标点、不加省略号，不拼接片段",
    )
    run_id: str = ""
    message_id: str = ""
    message_sha256: str = ""
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)
    quote_sha256: str = ""
    source_id: str = Field(
        default="", max_length=120, description="优先填写source_catalog中的片段id，由程序回填原文"
    )

    @field_validator("field", "value", "intent", "run_id", "message_id", "source_id", mode="before")
    @classmethod
    def trim_identifiers(cls, value):
        # Quotes must retain exact whitespace for character offsets and content hashes.
        return value.strip() if isinstance(value, str) else value


class FactEvidence(Base):
    """Durable source links; the bounded consultation summary is only a working view."""

    __tablename__ = "project_fact_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("delivery_projects.id"), index=True)
    recorded_run_id: Mapped[UUID] = mapped_column(ForeignKey("project_runs.id"), index=True)
    source: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    notes: DiscoveryUpdate = Field(default_factory=DiscoveryUpdate)
    sources: list[FactSource] = Field(default_factory=list, max_length=40)
    gaps: list[Gap] = Field(default_factory=list, max_length=15)
    conflicts: list[Gap] = Field(default_factory=list, max_length=10)
    reply: str = Field(min_length=1, max_length=24000)
    draft_request_quote: str = Field(default="", max_length=500)
    cancel_draft_quote: str = Field(default="", max_length=500)
    research_query: str = Field(default="", max_length=120)


class ConsultationTurn(Extraction):
    reply: str = Field(
        default="",
        max_length=24000,
        description="可选的顾问回复；缺少回复不影响保存有来源的业务信息。",
    )
    decision: DraftDecision = Field(default_factory=DraftDecision)

    @model_validator(mode="before")
    @classmethod
    def misplaced_envelope(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("notes"), dict):
            return value
        result, notes = dict(value), dict(value["notes"])
        for key in ("sources", "reply"):
            if key in notes:
                if key in result and result[key] != notes[key]:
                    raise ValueError("记录工具包含冲突的同名字段")
                result[key] = notes.pop(key)
        if "reply" in result and not isinstance(result["reply"], str):
            result["reply"] = ""
        return {**result, "notes": notes}


class ConsultationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    facts: list[FactSource] = Field(default_factory=list, max_length=40)
    remove_facts: list[FactSource] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=15)
    reply: str = ""
    decision: DraftDecision = Field(default_factory=DraftDecision)
    gaps: list[Gap] = Field(default_factory=list, max_length=15)
    conflicts: list[Gap] = Field(default_factory=list, max_length=10)

    @field_validator("facts", "remove_facts", mode="before")
    @classmethod
    def omit_empty_facts(cls, value):
        if not isinstance(value, list):
            return value
        return [
            row
            for row in value
            if not (
                isinstance(row, dict)
                and isinstance(row.get("value"), str)
                and not row["value"].strip()
            )
        ]

    @field_validator("reply", mode="before")
    @classmethod
    def optional_text(cls, value):
        return value if isinstance(value, str) else ""

    def turn(self):
        notes = {}
        lists = {"problems", "users", "goals", "scope", "constraints", "resolved_questions"}
        current_technology = [
            fact
            for fact in self.facts
            if fact.field == "technology_preference" and fact.source_id.startswith("current:")
        ]
        selected = [
            fact
            for fact in self.facts
            if fact.field != "technology_preference"
            or not current_technology
            or fact in current_technology
        ]
        technology = list(
            dict.fromkeys(fact.value for fact in selected if fact.field == "technology_preference")
        )
        origins = {
            ("id", fact.source_id.rsplit(":", 1)[0]) if fact.source_id else ("quote", fact.quote)
            for fact in selected
            if fact.field == "technology_preference"
        }
        if len(technology) > 1 and len(origins) > 1:
            raise ValueError("技术组合必须来自同一处客户原话")
        for fact in selected:
            if fact.field in lists:
                notes.setdefault(fact.field, []).append(fact.value)
            elif fact.field == "technology_preference":
                notes[fact.field] = "；".join(technology)
            elif fact.field in notes and notes[fact.field] != fact.value:
                raise ValueError("同一单值字段有相互冲突的信息")
            else:
                notes[fact.field] = fact.value
        removals = []
        for fact in self.remove_facts:
            if fact.field == "technology_preference" and fact.value in {
                "待确认",
                "未知",
                "未说明",
                "不确定",
            }:
                continue
            if fact.field not in lists - {"resolved_questions"}:
                if fact.field == "technology_preference":
                    if fact.field in notes:
                        continue
                    notes.setdefault("remove_facts", {}).setdefault(fact.field, []).append(
                        fact.value
                    )
                    removals.append(fact)
                    continue
                raise ValueError("该字段不能按列表纠正")
            notes.setdefault("remove_facts", {}).setdefault(fact.field, []).append(fact.value)
            removals.append(fact)
        if self.open_questions:
            notes["open_questions"] = self.open_questions
        return ConsultationTurn(
            notes=notes,
            sources=[*self.facts, *removals],
            reply=self.reply,
            decision=self.decision,
            gaps=self.gaps,
            conflicts=self.conflicts,
        )


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str = Field(max_length=300)
    url: str = Field(max_length=2000)
    snippet: str = Field(max_length=1200)
    fetched_at: str


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str = Field(min_length=1, max_length=1200)
    reference_id: str
    status: Literal["pending", "accepted", "rejected"] = "pending"


class ConsultationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["waiting_user", "waiting_search", "ready", "draft_ready"] = "waiting_user"
    draft_consent: bool = False
    gaps: list[Gap] = Field(default_factory=list, max_length=25)
    conflicts: list[Gap] = Field(default_factory=list, max_length=10)
    sources: list[FactSource] = Field(default_factory=list, max_length=100)
    references: list[Reference] = Field(default_factory=list, max_length=10)
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=10)
    last_run_id: str = ""
    discussion_run_id: str = ""
    memory_pending: bool = False
    document_pending: bool = False
    asked_questions: list[str] = Field(default_factory=list, max_length=30)
    checkpoint_thread: str = ""
    pending_search_query: str = Field(default="", max_length=120)
    draft_paused: bool = False
    last_draft_signature: str = ""
    draft_ledger: dict = Field(default_factory=dict)
    draft_assumptions: list[DraftAssumption] = Field(default_factory=list, max_length=20)

"""Internal Agent contracts. Identity is server supplied; these are not HTTP commands."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

AgentName = Literal["consultation", "drafting", "review", "project"]
DocumentAction = Literal["create", "revise", "fill", "rewrite_all"]
TaskAction = Literal["chat", "record", "create", "revise", "fill", "rewrite_all", "review"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskBudget(Contract):
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    deadline: AwareDatetime
    save_seconds: int = Field(default=0, ge=0)

    def remaining(self, *, models=0, tools=0, tokens=0):
        if min(models, tools, tokens) < 0:
            raise ValueError("消耗不能为负数")
        return TaskBudget(
            model_calls=max(0, self.model_calls - models),
            tool_calls=max(0, self.tool_calls - tools),
            tokens=max(0, self.tokens - tokens),
            deadline=self.deadline,
            save_seconds=self.save_seconds,
        )

    def seconds(self):
        return max(0, (self.deadline - datetime.now(UTC)).total_seconds() - self.save_seconds)


class AgentTask(Contract):
    schema_version: Literal[1] = 1
    task_id: UUID
    parent_task_id: UUID | None = None
    run_id: UUID | None = None
    project_id: UUID
    actor_id: UUID
    session_id: UUID = Field(repr=False)
    agent: AgentName
    action: TaskAction
    project_version: int = Field(ge=1)
    facts_hash: Digest
    item_id: UUID | None = None
    item_version: int | None = Field(default=None, ge=1)
    content_hash: Digest | None = None
    review_id: UUID | None = None
    source_ids: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        default=(), max_length=100
    )
    budget: TaskBudget
    allow_search: bool = False

    @model_validator(mode="after")
    def valid_scope(self):
        allowed = {
            "consultation": {"chat", "record"},
            "drafting": {"create", "revise", "fill", "rewrite_all"},
            "review": {"review"},
            "project": {"chat"},
        }
        if self.action not in allowed[self.agent]:
            raise ValueError("任务动作与智能体职责不匹配")
        if self.action == "record" and self.allow_search:
            raise ValueError("静默整理不能联网")
        if self.agent == "drafting" and (not self.parent_task_id or not self.source_ids):
            raise ValueError("需求生成必须有父任务及明确来源")
        if self.item_id is None and (
            self.item_version is not None or self.content_hash is not None
        ):
            raise ValueError("文档快照缺少目标")
        if self.action in {"revise", "fill", "review"} and not all(
            (self.item_id, self.item_version, self.content_hash)
        ):
            raise ValueError("此动作必须绑定完整文档快照")
        if (self.agent == "review") != (self.review_id is not None):
            raise ValueError("检查任务必须绑定检查编号")
        if (self.agent == "review") == (self.run_id is not None):
            raise ValueError("检查使用review_id，普通任务使用run_id")
        return self

    @property
    def fingerprint(self):
        # Budget decreases during execution; it is not part of the immutable input identity.
        return digest(self.model_dump(mode="json", exclude={"budget"}))

    def validate_scope(self, principal, project_id, run_id):
        if (
            self.project_id != project_id
            or self.run_id != run_id
            or self.actor_id != principal.account.id
            or self.session_id != principal.session_id
        ):
            raise ValueError("任务与当前项目或登录不匹配")

    def delegate_document(self, action: DocumentAction, *, budget, source_ids, item=None):
        if self.agent != "consultation" or self.action != "chat":
            raise ValueError("当前任务不能交接需求生成")
        if (
            budget.deadline > self.budget.deadline
            or budget.save_seconds < self.budget.save_seconds
            or any(
                getattr(budget, field) > getattr(self.budget, field)
                for field in ("model_calls", "tool_calls", "tokens")
            )
        ):
            raise ValueError("子任务不得扩大预算")
        data = self.model_dump(exclude={"task_id", "parent_task_id", "agent", "action", "budget"})
        data.update(
            source_ids=tuple(source_ids),
            item_id=UUID(str(item["id"])) if item else None,
            item_version=item["version"] if item else None,
            content_hash=hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            if item
            else None,
        )
        return AgentTask(
            **data,
            task_id=uuid5(self.task_id, "drafting"),
            parent_task_id=self.task_id,
            agent="drafting",
            action=action,
            budget=budget,
        )


class AgentResult(Contract):
    schema_version: Literal[1] = 1
    task_id: UUID
    run_id: UUID | None = None
    project_id: UUID
    input_fingerprint: Digest
    outcome: Literal["completed", "partial", "no_change", "conflict", "failed", "cancelled"]
    content_hash: Digest | None = None
    saved_project_version: int | None = Field(default=None, ge=1)
    stop_code: str = Field(default="", max_length=100)

    @classmethod
    def for_task(
        cls, task, *, outcome="completed", content=None, stop_code="", saved_project_version=None
    ):
        return cls(
            task_id=task.task_id,
            run_id=task.run_id,
            project_id=task.project_id,
            input_fingerprint=task.fingerprint,
            outcome=outcome,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content is not None
            else None,
            stop_code=stop_code,
            saved_project_version=saved_project_version,
        )

    def validate_for(self, task, *, content=None):
        if content is not None and self.saved_project_version is not None:
            raise ValueError("文档子图不能自报保存回执")
        expected = self.for_task(task, content=content, outcome=self.outcome)
        for field in ("task_id", "run_id", "project_id", "input_fingerprint", "content_hash"):
            if getattr(self, field) != getattr(expected, field):
                raise ValueError("交接结果与原任务或正文不匹配")

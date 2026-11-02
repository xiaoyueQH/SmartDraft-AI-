from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.common import supported_technical_fact, technology_has_source
from backend.models.consultation import Reference
from backend.models.drafting import DraftAssumption, DraftPlan, DraftSection, RevisionPlan
from backend.models.handoff import AgentTask


class GraphState(TypedDict):
    input_key: str
    draft: dict[str, Any]
    pending: list[dict[str, Any]]
    counters: dict[str, int]
    expires_at: float
    web_enabled: bool
    body: str
    partial: bool


class DraftState(BaseModel):
    """One invocation's shared data. Tools cannot change the customer facts."""

    model_config = ConfigDict(extra="forbid")
    business_notes: dict[str, Any] = Field(default_factory=dict)
    handoff: AgentTask | None = None
    handoff_counters: dict[str, int] = Field(default_factory=dict)
    user_request: str = ""
    customer_statements: list[str] = Field(default_factory=list)
    plan: DraftPlan | None = None
    sections: dict[str, str] = Field(default_factory=dict)
    section_specs: dict[str, DraftSection] = Field(default_factory=dict)
    failures: dict[str, str] = Field(default_factory=dict)
    references: list[Reference] = Field(default_factory=list)
    searched_topics: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[DraftAssumption] = Field(default_factory=list)
    previous_assumptions: list[DraftAssumption] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["working", "completed", "partial"] = "working"
    direct_body: str = ""
    stop_code: str = ""
    finalization_attempted: bool = False
    original_blocks: list[dict[str, Any]] = Field(default_factory=list)
    replacement: dict[str, Any] = Field(default_factory=dict)
    original_questions: list[str] = Field(default_factory=list)
    incremental: bool = False
    revision_plan: RevisionPlan | None = None
    read_ids: list[str] = Field(default_factory=list)
    verified: bool = False
    verification_attempted: bool = False
    no_change: bool = False
    fixed_protected: list[str] = Field(default_factory=list)

    def observations(self):
        """The current State is authoritative; keep the latest complete native tool exchange."""
        for index in range(len(self.messages) - 1, -1, -1):
            if self.messages[index]["role"] == "assistant":
                return self.messages[index:]
        return []

    def context(self, *, include_written=False):
        facts = dict(self.business_notes)
        selection = facts.get("technology_preference")
        unverified = {}
        if selection and not technology_has_source(selection, self.customer_statements):
            unverified["technology_preference"] = facts.pop("technology_preference")
        constraints = facts.get("constraints", [])
        facts["constraints"] = [
            value
            for value in constraints
            if supported_technical_fact(value, self.customer_statements, allow_excluded=True)
        ]
        if len(facts["constraints"]) != len(constraints):
            unverified["constraints"] = [v for v in constraints if v not in facts["constraints"]]
        data = {
            "user_request": self.user_request,
            "business_notes": facts,
            "unverified_historical_notes": unverified,
            "customer_statements": self.customer_statements,
            "role_terms": (self.plan.role_terms if self.plan else [])
            or self.business_notes.get("users", []),
            "design_rules": self.plan.design_rules if self.plan else [],
            "public_references": [ref.model_dump() for ref in self.references],
            "draft_assumptions": [item.model_dump() for item in self.assumptions],
            "previous_assumptions": [
                item.model_dump()
                for item in self.previous_assumptions
                if item not in self.assumptions
            ],
            "priority": "本次客户要求与最新已保存事实优先；历史补充方案仅供沿用，不得覆盖客户要求",
            "existing_sections": [
                {
                    "id": b["id"],
                    "title": b["title"],
                    "needs_completion": "本章暂未生成" in b["content"],
                }
                for b in self.original_blocks
            ],
            "revision_mode": bool(self.original_blocks),
            "incremental": self.incremental,
            "revision_plan": self.revision_plan.model_dump() if self.revision_plan else None,
            "read_block_ids": self.read_ids,
        }
        if include_written:
            data["written_sections"] = {
                sid: {
                    "title": self.section_specs[sid].title,
                    "excerpt": body if len(body) <= 400 else body[:160] + "\n…\n" + body[-240:],
                }
                for sid, body in self.sections.items()
            }
        return data

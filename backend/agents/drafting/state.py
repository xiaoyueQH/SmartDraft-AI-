from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.models.consultation import Reference
from backend.models.drafting import DraftAssumption, DraftPlan, DraftSection


class DraftState(BaseModel):
    """One invocation's shared data. Tools cannot change the customer facts."""

    model_config = ConfigDict(extra="forbid")
    business_notes: dict[str, Any] = Field(default_factory=dict)
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

    def context(self, *, include_written=False):
        data = {
            "user_request": self.user_request,
            "business_notes": self.business_notes,
            "customer_statements": self.customer_statements,
            "role_terms": (self.plan.role_terms if self.plan else [])
            or self.business_notes.get("users", []),
            "public_references": [ref.model_dump() for ref in self.references],
            "draft_assumptions": [item.model_dump() for item in self.assumptions],
            "previous_assumptions": [item.model_dump() for item in self.previous_assumptions],
            "priority": "本次客户要求与最新已保存事实优先；历史补充方案仅供沿用，不得覆盖客户要求",
        }
        if include_written:
            data["written_sections"] = {
                sid: {
                    "title": self.section_specs[sid].title,
                    "excerpt": body if len(body) <= 1200 else body[:400] + "\n…\n" + body[-800:],
                }
                for sid, body in self.sections.items()
            }
        return data

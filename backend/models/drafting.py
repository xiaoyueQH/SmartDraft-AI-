from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class DraftDecision(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    request_quote: str = Field(default="", max_length=500)
    pause_quote: str = Field(default="", max_length=500)


class DraftSection(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: str = Field(default="", max_length=40)
    title: str = Field(min_length=1, max_length=80)
    focus: str = Field(default="", max_length=500)


class DraftAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: Literal["technology", "workflow", "validation", "operations", "other"]
    topic: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1200)
    rationale: str = Field(default="", max_length=500)


class WriteSection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    section_id: str = Field(
        min_length=1, max_length=40, validation_alias=AliasChoices("section_id", "id")
    )
    title: str = Field(default="", max_length=80, description="仅未建立大纲时填写章名")
    focus: str = Field(default="", max_length=500, description="额外写作重点，可留空")


class DraftPlan(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    sections: list[DraftSection] = Field(min_length=1, max_length=6)
    role_terms: list[str] = Field(default_factory=list, max_length=15)
    design_rules: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        default_factory=list,
        max_length=8,
        description="跨章节共用的简短设计约定：角色权限、表与字段命名、状态、时间边界、接口规则。不是客户确认事实；最新客户要求优先。",
    )
    pending_questions: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[DraftAssumption] = Field(
        default_factory=list,
        max_length=20,
        description="仅记录用户未明确选择、由Agent自动补齐的设计建议；不要复制用户已指定的事实或技术选择。",
    )
    resolved_questions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def distinct_sections(self):
        for index, section in enumerate(self.sections, 1):
            if not section.id:
                section.id = f"section-{index}"
            if not section.focus:
                section.focus = section.title
        if len({s.id for s in self.sections}) != len(self.sections):
            raise ValueError("章节编号重复")
        return self


class DraftFinish(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    questions: list[str] = Field(default_factory=list, max_length=40)
    resolved_questions: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[DraftAssumption] = Field(
        default_factory=list,
        max_length=20,
        description="仅汇总自动生成的实现选择，不得把客户已指定的内容标为自动选择。",
    )

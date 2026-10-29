from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ModelPurpose = Literal["consultation", "draft_planner", "draft_writer", "draft_reviewer"]


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = Field(default="", max_length=120, pattern=r"^[a-zA-Z0-9_.:/-]*$")
    concurrency: int = Field(default=2, ge=1, le=16)
    wait_seconds: float = Field(default=0.5, gt=0, le=5)
    temperature: float = Field(default=0.2, ge=0, le=2)
    supports_tools: bool = True
    supports_json: bool = True


class ModelError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)

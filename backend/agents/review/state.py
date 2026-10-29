from typing import Any, TypedDict


class ReviewState(TypedDict):
    blocks: list[dict[str, Any]]
    read_ids: list[str]
    findings: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    rounds: int
    calls: int
    tokens: int
    complete: bool
    partial: bool
    verified: bool
    summary: str
    expires_at: float
    decision: dict[str, Any]
    result: dict[str, Any]
    stop_reasons: list[str]
    last_invalid_call: str
    read_source_ids: list[str]
    loaded_sources: list[dict[str, Any]]

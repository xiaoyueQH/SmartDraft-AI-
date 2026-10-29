from typing import TypedDict


class GraphState(TypedDict):
    messages: list[dict]
    rounds: int
    calls: int
    pending: list[dict]
    answer: str
    tokens: int
    drafts: list[dict]
    notes: list[dict]

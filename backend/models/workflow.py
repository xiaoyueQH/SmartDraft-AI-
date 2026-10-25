from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints, field_validator

Action = Literal[
    "pay",
    "ship",
    "receive",
    "return_request",
    "return_review",
    "return_ship",
    "return_receive",
    "return_refund",
]
ReturnStatus = Literal["requested", "approved", "rejected", "returning", "received", "closed"]
ADMIN_ACTIONS = frozenset({"ship", "return_review", "return_receive", "return_refund"})


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation_id: UUID
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("请明确确认操作")
        return value


class ShipmentCommand(Confirmation):
    carrier: Annotated[str, StringConstraints(min_length=2, max_length=64)]
    tracking_number: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{3,64}$")]


class ReturnCommand(Confirmation):
    reason: Annotated[str, StringConstraints(min_length=3, max_length=300)]


class ReviewCommand(ReturnCommand):
    decision: Literal["approve", "reject"]


class WorkflowReceipt(BaseModel):
    action: Action
    order_id: UUID
    order_status: str
    case_id: UUID | None
    case_status: ReturnStatus | None
    occurred_at: datetime


class WorkflowView(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class PaymentView(WorkflowView):
    id: UUID
    amount_cents: int
    channel: str
    paid_at: datetime


class RefundView(WorkflowView):
    id: UUID
    amount_cents: int
    channel: str
    refunded_at: datetime


class ReturnView(WorkflowView):
    id: UUID
    order_id: UUID
    status: ReturnStatus
    reason: str
    review_note: str
    carrier: str | None
    tracking_number: str | None
    refund_cents: int
    created_at: datetime
    updated_at: datetime
    restocked_at: datetime | None


class EventView(WorkflowView):
    id: UUID
    action: str
    description: str
    actor_username: str
    occurred_at: datetime


class ReturnSummary(ReturnView):
    number: str
    customer_username: str


class ReturnPage(BaseModel):
    items: list[ReturnSummary]
    has_more: bool

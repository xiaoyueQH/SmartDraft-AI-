from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class View(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    @field_validator("expires_at", "completed_at", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CancellationReason(View):
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写取消原因")
        return value


class CancellationCommit(View):
    confirmation_id: UUID
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("必须明确确认取消")
        return value


class ReleaseItem(View):
    product_id: UUID
    product_name: str
    quantity: int


class CancellationPreview(View):
    confirmation_id: UUID
    order_id: UUID
    number: str
    from_status: Literal["pending_payment", "paid"]
    reason: str
    total_cents: int
    expires_at: datetime
    release_items: list[ReleaseItem]
    refund_status: Literal["not_required", "simulated"]
    refund_cents: int
    is_demo: bool


class CancellationReceipt(View):
    confirmation_id: UUID
    order_id: UUID
    actor_username: str
    actor_role: str
    from_status: Literal["pending_payment", "paid"]
    reason: str
    completed_at: datetime
    released_units: int
    released_items: list[ReleaseItem]
    refund_status: Literal["not_required", "simulated"]
    refund_cents: int
    is_demo: bool


class CancellationError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def check_cancellable(status: str, is_demo: bool, has_shipments: bool) -> None:
    if status not in {"pending_payment", "paid"}:
        raise CancellationError("ORDER_NOT_CANCELLABLE", "当前订单状态不允许取消，请刷新订单")
    if has_shipments:
        raise CancellationError("SHIPMENT_EXISTS", "订单已生成物流记录，不能直接取消")
    if status == "paid" and not is_demo:
        raise CancellationError("REFUND_UNAVAILABLE", "真实已付款订单需接入退款流程后处理")

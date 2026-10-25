from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.models.orders import OrderDetail


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    product_id: UUID
    quantity: int = Field(strict=True, ge=1, le=99)
    expected_price_cents: int = Field(strict=True, ge=1, le=100_000_000)


class CheckoutResult(BaseModel):
    order: OrderDetail
    created: bool

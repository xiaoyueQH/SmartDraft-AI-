from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.models.cancellation import CancellationReceipt
from backend.models.workflow import EventView, PaymentView, RefundView, ReturnView

OrderStatus = Literal["pending_payment", "paid", "shipped", "delivered", "cancelled"]


class View(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class ProductView(View):
    id: UUID
    sku: str
    name: str
    description: str
    price_cents: int
    stock: int
    is_demo: bool
    is_active: bool = True
    deleted_at: datetime | None = None
    version: int = 1


class OrderSummary(View):
    id: UUID
    number: str
    customer_username: str
    status: OrderStatus
    total_cents: int
    created_at: datetime
    is_demo: bool


class ItemView(View):
    id: UUID
    product_id: UUID
    sku: str
    product_name: str
    unit_price_cents: int
    quantity: int


class ShipmentEventView(View):
    id: UUID
    occurred_at: datetime
    description: str


class ShipmentView(View):
    id: UUID
    carrier: str
    tracking_number: str
    is_demo: bool
    events: list[ShipmentEventView]


class OrderDetail(OrderSummary):
    items: list[ItemView]
    shipments: list[ShipmentView]
    cancellation: CancellationReceipt | None = None
    payment: PaymentView | None = None
    refund: RefundView | None = None
    after_sale: ReturnView | None = None
    events: list[EventView] = []


class OrderPage(View):
    items: list[OrderSummary]
    has_more: bool


class ProductPage(View):
    items: list[ProductView]
    has_more: bool


class OrderNotFound(Exception):
    pass

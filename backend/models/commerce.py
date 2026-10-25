from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_products_price"),
        CheckConstraint("stock >= 0", name="ck_products_stock"),
        CheckConstraint("version > 0", name="ck_products_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class ProductChange(Base):
    __tablename__ = "product_changes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CatalogImport(Base):
    __tablename__ = "catalog_imports"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), unique=True, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candidate_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_payment', 'paid', 'shipped', 'delivered', 'cancelled')",
            name="ck_orders_status",
        ),
        CheckConstraint("total_cents >= 0", name="ck_orders_total"),
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        Index("ix_orders_created", "created_at", "id"),
        Index("ix_orders_status_created", "status", "created_at", "id"),
        UniqueConstraint("user_id", "request_id", name="uq_orders_user_request"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity"),
        CheckConstraint("unit_price_cents >= 0", name="ck_order_items_price"),
        Index("ix_order_items_order", "order_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (Index("ix_shipments_order", "order_id", "id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    carrier: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"
    __table_args__ = (Index("ix_shipment_events_time", "shipment_id", "occurred_at", "id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_reservations_quantity"),
        CheckConstraint(
            "released_at IS NULL OR consumed_at IS NULL", name="ck_reservations_terminal"
        ),
    )

    order_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_items.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CancellationConfirmation(Base):
    __tablename__ = "cancellation_confirmations"
    __table_args__ = (Index("ix_cancel_confirmations_owner", "order_id", "actor_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("auth_sessions.id"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderCancellation(Base):
    __tablename__ = "order_cancellations"
    __table_args__ = (
        CheckConstraint("released_units > 0", name="ck_cancellations_released"),
        CheckConstraint("refund_cents >= 0", name="ck_cancellations_refund"),
        CheckConstraint(
            "refund_status IN ('not_required', 'simulated')", name="ck_cancellations_refund_status"
        ),
    )

    confirmation_id: Mapped[UUID] = mapped_column(
        ForeignKey("cancellation_confirmations.id"), primary_key=True
    )
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_units: Mapped[int] = mapped_column(Integer, nullable=False)
    released_items: Mapped[list] = mapped_column(JSON, nullable=False)
    refund_status: Mapped[str] = mapped_column(String(24), nullable=False)
    refund_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)

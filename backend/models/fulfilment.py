from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.orm import Base


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_payments_amount"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReturnCase(Base):
    __tablename__ = "return_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','approved','rejected','returning','received','closed')",
            name="ck_returns_status",
        ),
        CheckConstraint("refund_cents > 0", name="ck_returns_amount"),
        Index("ix_returns_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    review_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    carrier: Mapped[str | None] = mapped_column(String(64))
    tracking_number: Mapped[str | None] = mapped_column(String(64))
    refund_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_refunds_amount"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    case_id: Mapped[UUID | None] = mapped_column(ForeignKey("return_cases.id"), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    refunded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_time", "order_id", "occurred_at", "id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowAction(Base):
    __tablename__ = "workflow_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

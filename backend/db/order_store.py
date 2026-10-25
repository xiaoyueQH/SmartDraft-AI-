from uuid import UUID

from sqlalchemy import select, text

from backend.db.catalog_store import CatalogStore
from backend.db.session import Database
from backend.models.cancellation import CancellationReceipt
from backend.models.commerce import (
    Order,
    OrderCancellation,
    OrderItem,
    Shipment,
    ShipmentEvent,
)
from backend.models.fulfilment import OrderEvent, Payment, Refund, ReturnCase
from backend.models.orders import (
    ItemView,
    OrderDetail,
    OrderNotFound,
    OrderPage,
    OrderStatus,
    OrderSummary,
    ProductPage,
    ShipmentEventView,
    ShipmentView,
)
from backend.models.orm import User
from backend.models.workflow import EventView, PaymentView, RefundView, ReturnView


def summary(order: Order, username: str) -> OrderSummary:
    return OrderSummary(
        id=order.id,
        number=order.number,
        customer_username=username,
        status=order.status,
        total_cents=order.total_cents,
        created_at=order.created_at,
        is_demo=order.is_demo,
    )


class OrderStore:
    def __init__(self, database: Database):
        self.database = database

    async def products(self, limit: int, offset: int) -> ProductPage:
        return await CatalogStore(self.database).products(limit, offset)

    async def list_orders(
        self,
        owner_id: UUID | None,
        status: OrderStatus | None,
        number: str | None,
        limit: int,
        offset: int,
    ) -> OrderPage:
        statement = select(Order, User.username).join(User, User.id == Order.user_id)
        if owner_id is not None:
            statement = statement.where(Order.user_id == owner_id)
        if status:
            statement = statement.where(Order.status == status)
        if number:
            statement = statement.where(Order.number == number)
        statement = statement.order_by(Order.created_at.desc(), Order.id.desc())
        async with self.database.session() as session:
            rows = (await session.execute(statement.limit(limit + 1).offset(offset))).all()
            return OrderPage(
                items=[summary(order, username) for order, username in rows[:limit]],
                has_more=len(rows) > limit,
            )

    async def detail(self, order_id: UUID, owner_id: UUID | None) -> OrderDetail:
        statement = (
            select(Order, User.username, Payment, Refund, ReturnCase, OrderCancellation)
            .join(User, User.id == Order.user_id)
            .outerjoin(Payment, Payment.order_id == Order.id)
            .outerjoin(Refund, Refund.order_id == Order.id)
            .outerjoin(ReturnCase, ReturnCase.order_id == Order.id)
            .outerjoin(OrderCancellation, OrderCancellation.order_id == Order.id)
            .where(Order.id == order_id)
        )
        if owner_id is not None:
            statement = statement.where(Order.user_id == owner_id)
        async with self.database.session() as session:
            if self.database.engine.dialect.name == "postgresql":
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                raise OrderNotFound
            order, username, payment, refund, case, cancellation = row
            items = await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
            )
            shipments = list(
                await session.scalars(
                    select(Shipment).where(Shipment.order_id == order.id).order_by(Shipment.id)
                )
            )
            events = {shipment.id: [] for shipment in shipments}
            if shipments:
                for event in await session.scalars(
                    select(ShipmentEvent)
                    .where(ShipmentEvent.shipment_id.in_(events))
                    .order_by(ShipmentEvent.occurred_at, ShipmentEvent.id)
                ):
                    events[event.shipment_id].append(ShipmentEventView.model_validate(event))
            return OrderDetail(
                **summary(order, username).model_dump(),
                payment=PaymentView.model_validate(payment) if payment else None,
                refund=RefundView.model_validate(refund) if refund else None,
                after_sale=ReturnView.model_validate(case) if case else None,
                events=[
                    EventView.model_validate(row)
                    for row in await session.scalars(
                        select(OrderEvent)
                        .where(OrderEvent.order_id == order.id)
                        .order_by(OrderEvent.occurred_at, OrderEvent.id)
                    )
                ],
                cancellation=(
                    CancellationReceipt.model_validate(cancellation) if cancellation else None
                ),
                items=[ItemView.model_validate(item) for item in items],
                shipments=[
                    ShipmentView(
                        id=shipment.id,
                        carrier=shipment.carrier,
                        tracking_number=shipment.tracking_number,
                        is_demo=shipment.is_demo,
                        events=events[shipment.id],
                    )
                    for shipment in shipments
                ],
            )
